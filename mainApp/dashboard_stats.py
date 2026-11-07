"""
Numbers for the home dashboard (templates/dashboard/index.html).

school_dashboard(school)  -> one school's overview (head teacher / ADMIN view)
bursar_dashboard(school)  -> one school's fees (BURSAR view)
platform_dashboard()      -> every school on the system (Muzadi / SUPER_ADMIN view)

Every block is wrapped so that one failing query (e.g. a module whose tables are
missing on an old database) never breaks the whole dashboard.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal

from django.db.models import Count, DecimalField, F, Q, Sum, Value
from django.db.models.functions import Coalesce, TruncMonth
from django.utils import timezone

logger = logging.getLogger("mainApp.dashboard")

ZERO = Decimal("0")
MONEY = DecimalField(max_digits=14, decimal_places=2)


def _safe(fn, default):
    try:
        return fn()
    except Exception:  # pragma: no cover - logged, dashboard keeps rendering
        logger.exception("dashboard block failed: %s", getattr(fn, "__name__", fn))
        return default


def _num(value):
    """Decimal/None -> int (UGX amounts are shown without decimals)."""
    return int(value or 0)


def _pct(part, whole):
    return round((part / whole) * 100, 1) if whole else 0


def _month_starts(today, n):
    """First day of the last n months, oldest first."""
    out = []
    y, m = today.year, today.month
    for _ in range(n):
        out.append(today.replace(year=y, month=m, day=1))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return list(reversed(out))


def _month_key(d):
    if hasattr(d, "date"):
        d = timezone.localtime(d).date() if timezone.is_aware(d) else d.date()
    return (d.year, d.month)


# =============================================================================
# ONE SCHOOL
# =============================================================================
def school_dashboard(school, today=None):
    from mainApp.models.calendar import Term

    today = today or timezone.localdate()
    term = getattr(school, "current_term", None) or Term.objects.filter(is_current=True).order_by("-id").first()

    data = {"term": term}
    data.update(_safe(lambda: _students_block(school, today), {}))
    data.update(_safe(lambda: _staff_block(school), {}))
    data["fees"] = _safe(lambda: _fees_block(school, term, today), None)
    data["attendance"] = _safe(lambda: _attendance_block(school, today), None)
    data["upcoming"] = _safe(lambda: _upcoming_block(school, today), [])
    data["setup"] = _safe(lambda: _setup_block(school), None)
    data["sms_account"] = _safe(lambda: _sms_account(school), None)
    return data


def _students_block(school, today):
    from mainApp.models.student import Student

    qs = Student.objects.filter(school=school)
    active = qs.filter(is_active=True)
    total = qs.count()
    n_active = active.count()
    week_start = today - timedelta(days=7)
    month_start = today.replace(day=1)

    by_gender = dict(active.order_by().values("gender").annotate(n=Count("id")).values_list("gender", "n"))
    male, female = by_gender.get("M", 0), by_gender.get("F", 0)

    per_class = (active.order_by().values("current_class__name", "current_class__short_name", "current_class__level__order")
                 .annotate(total=Count("id"),
                           boys=Count("id", filter=Q(gender="M")),
                           girls=Count("id", filter=Q(gender="F")))
                 .order_by("current_class__level__order", "current_class__name"))
    class_labels, class_boys, class_girls, class_totals = [], [], [], []
    for row in per_class:
        class_labels.append(row["current_class__short_name"] or row["current_class__name"] or "Unassigned")
        class_boys.append(row["boys"])
        class_girls.append(row["girls"])
        class_totals.append(row["total"])

    per_level = (active.order_by().values("level__code", "level__name").annotate(total=Count("id"))
                 .order_by("level__code"))
    level_rows = [{"label": r["level__code"] or r["level__name"] or "Unknown", "total": r["total"],
                   "pct": _pct(r["total"], n_active)} for r in per_level]

    # Enrolment over the last 6 months (by enrolled date)
    months = _month_starts(today, 6)
    enrol = {_month_key(r["m"]): r["n"] for r in
             qs.filter(enrolled_date__gte=months[0]).order_by().annotate(m=TruncMonth("enrolled_date"))
             .values("m").annotate(n=Count("id")).values("m", "n") if r["m"]}

    return {
        "students_total": total,
        "students_active": n_active,
        "students_inactive": total - n_active,
        "students_new_week": qs.filter(enrolled_date__gte=week_start).count(),
        "students_new_month": qs.filter(enrolled_date__gte=month_start).count(),
        "students_male": male,
        "students_female": female,
        "students_male_pct": _pct(male, n_active),
        "students_female_pct": _pct(female, n_active),
        "class_rows": [{"label": l, "boys": b, "girls": g, "total": t}
                       for l, b, g, t in zip(class_labels, class_boys, class_girls, class_totals)],
        "level_rows": level_rows,
        "chart_classes": {"labels": class_labels, "boys": class_boys, "girls": class_girls},
        "chart_enrolment": {"labels": [m.strftime("%b") for m in months],
                            "values": [enrol.get((m.year, m.month), 0) for m in months]},
    }


def _staff_block(school):
    from mainApp.models.models import Class, Profile, Stream
    from mainApp.models.teacher_models import Teacher

    roles = dict(Profile.objects.filter(school=school).order_by().values("role")
                 .annotate(n=Count("id")).values_list("role", "n"))
    return {
        "teachers_total": Teacher.objects.filter(user__profile__school=school).count(),
        "classes_total": Class.objects.filter(school=school).count(),
        "streams_total": Stream.objects.filter(class_ref__school=school).count(),
        "parents_total": roles.get("PARENT", 0),
        "portal_students": roles.get("STUDENT", 0),
        "admins_total": roles.get("ADMIN", 0) + roles.get("BURSAR", 0),
    }


def _fees_block(school, term, today):
    from mainApp.models.fees_models import StudentFeeCharge, StudentFeePayment

    payments = StudentFeePayment.objects.filter(school=school, is_reversed=False)
    term_payments = payments.filter(term=term) if term else payments.none()

    expected = paid = ZERO
    if term:
        agg = StudentFeeCharge.objects.filter(school=school, term=term).aggregate(
            amount=Coalesce(Sum("amount"), Value(ZERO), output_field=MONEY),
            surcharge=Coalesce(Sum("surcharge_amount"), Value(ZERO), output_field=MONEY),
            waiver=Coalesce(Sum("waiver_amount"), Value(ZERO), output_field=MONEY),
            paid=Coalesce(Sum("amount_paid"), Value(ZERO), output_field=MONEY),
        )
        expected = agg["amount"] + agg["surcharge"] - agg["waiver"]
        paid = agg["paid"]
    outstanding = max(expected - paid, ZERO)

    def total(qs):
        return qs.aggregate(t=Coalesce(Sum("amount"), Value(ZERO), output_field=MONEY))["t"]

    collected_term = total(term_payments)
    collected_today = total(payments.filter(payment_date=today))
    week_start = today - timedelta(days=today.weekday())
    collected_week = total(payments.filter(payment_date__gte=week_start))

    # Weekly collections, last 8 weeks (Mon-Sun)
    labels, values = [], []
    for i in range(7, -1, -1):
        start = week_start - timedelta(weeks=i)
        end = start + timedelta(days=6)
        labels.append(start.strftime("%d %b"))
        values.append(_num(total(payments.filter(payment_date__range=(start, end)))))

    by_method = list(term_payments.order_by().values("payment_method")
                     .annotate(t=Sum("amount"), n=Count("id")).order_by("-t"))
    method_names = dict(StudentFeePayment._meta.get_field("payment_method").choices)
    methods = [{"code": m["payment_method"], "label": method_names.get(m["payment_method"], m["payment_method"]),
                "amount": _num(m["t"]), "count": m["n"], "pct": _pct(_num(m["t"]), _num(collected_term))}
               for m in by_method]

    recent = list(payments.select_related("student", "student__current_class").order_by("-payment_date", "-id")[:6])

    defaulters = []
    if term:
        balance_expr = F("amount") + F("surcharge_amount") - F("waiver_amount") - F("amount_paid")
        rows = (StudentFeeCharge.objects.filter(school=school, term=term, student__is_active=True)
                .order_by().values("student_id", "student__first_name", "student__last_name",
                        "student__current_class__short_name", "student__current_class__name")
                .annotate(balance=Sum(balance_expr, output_field=MONEY))
                .filter(balance__gt=0).order_by("-balance")[:5])
        for r in rows:
            defaulters.append({
                "id": r["student_id"],
                "name": f'{r["student__first_name"]} {r["student__last_name"]}'.strip(),
                "class_name": r["student__current_class__short_name"] or r["student__current_class__name"] or "",
                "balance": _num(r["balance"]),
            })

    debtors = 0
    if term:
        debtors = (StudentFeeCharge.objects.filter(school=school, term=term, status__in=("PENDING", "PARTIAL"))
                   .order_by().values("student_id").distinct().count())

    return {
        "expected": _num(expected),
        "paid": _num(paid),
        "outstanding": _num(outstanding),
        "collection_rate": _pct(_num(paid), _num(expected)),
        "collected_term": _num(collected_term),
        "collected_today": _num(collected_today),
        "collected_week": _num(collected_week),
        "payments_term": term_payments.count(),
        "debtors": debtors,
        "methods": methods,
        "recent": recent,
        "defaulters": defaulters,
        "chart": {"labels": labels, "values": values,
                  "method_labels": [m["label"] for m in methods],
                  "method_values": [m["amount"] for m in methods]},
    }


# =============================================================================
# BURSAR (fees only)
# =============================================================================
def bursar_dashboard(school, today=None):
    """Fee dashboard for the bursar: this term's money, per class, per fee item."""
    from mainApp.models.calendar import Term

    today = today or timezone.localdate()
    term = getattr(school, "current_term", None) or Term.objects.filter(is_current=True).order_by("-id").first()

    data = {"term": term}
    data["fees"] = _safe(lambda: _fees_block(school, term, today), None)
    data.update(_safe(lambda: _bursar_block(school, term, today), {}))
    return data


def _bursar_block(school, term, today):
    from mainApp.models.fees_models import StudentFeeAdjustment, StudentFeeCharge, StudentFeePayment

    balance_expr = F("amount") + F("surcharge_amount") - F("waiver_amount") - F("amount_paid")
    net_expr = F("amount") + F("surcharge_amount") - F("waiver_amount")
    term_charges = (StudentFeeCharge.objects.filter(school=school, term=term)
                    if term else StudentFeeCharge.objects.none())

    # ---- Per class: expected vs collected ---------------------------------
    class_rows = []
    for r in (term_charges.order_by()
              .values("class_ref__name", "class_ref__short_name", "class_ref__level__order")
              .annotate(expected=Sum(net_expr, output_field=MONEY),
                        paid=Coalesce(Sum("amount_paid"), Value(ZERO), output_field=MONEY),
                        students=Count("student_id", distinct=True))
              .order_by("class_ref__level__order", "class_ref__name")):
        expected, paid = _num(r["expected"]), _num(r["paid"])
        class_rows.append({
            "label": r["class_ref__short_name"] or r["class_ref__name"] or "No class",
            "students": r["students"],
            "expected": expected,
            "paid": paid,
            "outstanding": max(expected - paid, 0),
            "rate": _pct(paid, expected),
        })

    # ---- Per fee item (tuition, uniform, ...) -----------------------------
    category_rows = []
    for r in (term_charges.order_by().values("fee_category__name")
              .annotate(expected=Sum(net_expr, output_field=MONEY),
                        paid=Coalesce(Sum("amount_paid"), Value(ZERO), output_field=MONEY))
              .order_by("-expected")):
        expected, paid = _num(r["expected"]), _num(r["paid"])
        category_rows.append({"label": r["fee_category__name"] or "Other", "expected": expected,
                              "paid": paid, "outstanding": max(expected - paid, 0),
                              "rate": _pct(paid, expected)})

    # ---- Students: fully paid / part paid / nothing paid -------------------
    per_student = list(term_charges.filter(student__is_active=True).order_by().values("student_id")
                       .annotate(due=Sum(net_expr, output_field=MONEY),
                                 paid=Coalesce(Sum("amount_paid"), Value(ZERO), output_field=MONEY)))
    cleared = partial = unpaid = 0
    for r in per_student:
        due, paid = r["due"] or ZERO, r["paid"] or ZERO
        if due <= 0 or paid >= due:
            cleared += 1
        elif paid > 0:
            partial += 1
        else:
            unpaid += 1
    billed = cleared + partial + unpaid

    # ---- Arrears carried from earlier terms --------------------------------
    old = StudentFeeCharge.objects.filter(school=school, student__is_active=True)
    if term:
        old = old.exclude(term=term)
    arrears = old.order_by().aggregate(t=Coalesce(Sum(balance_expr, output_field=MONEY), Value(ZERO),
                                                  output_field=MONEY))["t"]
    arrears = max(_num(arrears), 0)

    # ---- Term progress: collected so far vs the target ---------------------
    expected_total = sum(r["expected"] for r in class_rows)
    progress = {"labels": [], "collected": [], "target": [], "projected": 0, "days_total": 0,
                "days_gone": 0, "daily_needed": 0}
    start = getattr(term, "start_date", None)
    end = getattr(term, "end_date", None)
    if term and start and end and end >= start:
        payments = StudentFeePayment.objects.filter(school=school, term=term, is_reversed=False)
        by_day = dict(payments.order_by().values("payment_date").annotate(t=Sum("amount"))
                      .values_list("payment_date", "t"))
        # anything received before the term opened counts on day one
        early = sum((v or ZERO) for d, v in by_day.items() if d and d < start)

        days_total = (end - start).days + 1
        last = min(max(today, start), end)
        days_gone = (last - start).days + 1
        week_starts = []
        d = start
        while d <= end:
            week_starts.append(d)
            d += timedelta(days=7)

        running = early
        cursor = start
        for i, ws in enumerate(week_starts):
            we = min(ws + timedelta(days=6), end)
            progress["labels"].append(f"Wk {i + 1}")
            # straight line from 0 to 100% of expected by the last day of term
            progress["target"].append(_num(Decimal(expected_total) * Decimal((we - start).days + 1) / days_total))
            if ws > last:
                progress["collected"].append(None)
                continue
            while cursor <= min(we, last):
                running += by_day.get(cursor) or ZERO
                cursor += timedelta(days=1)
            progress["collected"].append(_num(running))

        collected = _num(running)
        projected = int(collected / days_gone * days_total) if days_gone else collected
        days_left = max((end - max(today, start)).days, 0) if today <= end else 0
        remaining = max(expected_total - collected, 0)
        progress.update({
            "projected": min(projected, expected_total) if expected_total else projected,
            "projected_rate": _pct(min(projected, expected_total), expected_total),
            "days_total": days_total,
            "days_gone": days_gone,
            "days_left": days_left,
            "daily_needed": int(remaining / days_left) if days_left else remaining,
            "target_today": int(expected_total * days_gone / days_total),
        })
        progress["on_track"] = collected >= progress["target_today"]
        progress["gap"] = max(progress["target_today"] - collected, 0)

    # ---- Discounts waiting for an admin (requested by the bursar) ----------
    pending_adjustments = StudentFeeAdjustment.objects.filter(charge__school=school, status="PENDING").count()

    # ---- Last 30 days, day by day ------------------------------------------
    start30 = today - timedelta(days=29)
    daily = dict(StudentFeePayment.objects.filter(school=school, is_reversed=False,
                                                  payment_date__range=(start30, today))
                 .order_by().values("payment_date").annotate(t=Sum("amount")).values_list("payment_date", "t"))
    daily_labels, daily_values = [], []
    for i in range(30):
        d = start30 + timedelta(days=i)
        daily_labels.append(d.strftime("%d %b"))
        daily_values.append(_num(daily.get(d)))

    return {
        "class_rows": class_rows,
        "category_rows": category_rows,
        "students_billed": billed,
        "students_cleared": cleared,
        "students_partial": partial,
        "students_unpaid": unpaid,
        "students_cleared_pct": _pct(cleared, billed),
        "arrears": arrears,
        "pending_adjustments": pending_adjustments,
        "progress": progress,
        "chart_bursar": {
            "classes": {"labels": [r["label"] for r in class_rows],
                        "expected": [r["expected"] for r in class_rows],
                        "paid": [r["paid"] for r in class_rows]},
            "categories": {"labels": [r["label"] for r in category_rows],
                           "expected": [r["expected"] for r in category_rows],
                           "paid": [r["paid"] for r in category_rows]},
            "status": {"labels": ["Fully paid", "Part paid", "Not paid"],
                       "values": [cleared, partial, unpaid]},
            "progress": {"labels": progress["labels"], "collected": progress["collected"],
                         "target": progress["target"]},
            "daily": {"labels": daily_labels, "values": daily_values},
        },
    }


def _attendance_block(school, today):
    from mainApp.models.attendance_models import AttendanceRecord, AttendanceSession

    records = AttendanceRecord.objects.filter(session__school=school)
    today_counts = dict(records.filter(session__date=today).order_by().values("status")
                        .annotate(n=Count("id")).values_list("status", "n"))
    present, absent, late = today_counts.get("P", 0), today_counts.get("A", 0), today_counts.get("L", 0)
    marked = present + absent + late

    start = today - timedelta(days=13)
    daily = {}
    for d, status, n in (records.filter(session__date__range=(start, today))
                         .order_by().values("session__date", "status").annotate(n=Count("id"))
                         .values_list("session__date", "status", "n")):
        daily.setdefault(d, {"P": 0, "A": 0, "L": 0})[status] = n
    labels, rates = [], []
    for i in range(14):
        d = start + timedelta(days=i)
        if d.weekday() == 6 and d not in daily:  # skip empty Sundays
            continue
        c = daily.get(d)
        labels.append(d.strftime("%a %d"))
        rates.append(_pct(c["P"] + c["L"], c["P"] + c["A"] + c["L"]) if c else None)

    sessions_today = AttendanceSession.objects.filter(school=school, date=today)
    return {
        "present": present, "absent": absent, "late": late, "marked": marked,
        "rate": _pct(present + late, marked),
        "sessions_today": sessions_today.count(),
        "classes_marked": sessions_today.order_by().values("class_ref_id", "stream_id").distinct().count(),
        "chart": {"labels": labels, "values": rates},
    }


def _upcoming_block(school, today):
    from mainApp.models.calendar import Holiday, SchoolEvent

    items = []
    for e in SchoolEvent.objects.filter(school=school, end_date__gte=today).order_by("start_date")[:6]:
        items.append({"title": e.title, "start": e.start_date, "end": e.end_date,
                      "kind": e.get_event_type_display(), "venue": e.venue, "is_holiday": False,
                      "days": (e.start_date - today).days})
    holidays = Holiday.objects.filter(Q(school=school) | Q(school__isnull=True),
                                      date__gte=today, date__lte=today + timedelta(days=60)).order_by("date")[:4]
    for h in holidays:
        items.append({"title": h.name, "start": h.date, "end": h.end_date or h.date,
                      "kind": "Holiday", "venue": "", "is_holiday": True, "days": (h.date - today).days})
    items.sort(key=lambda x: x["start"])
    return items[:6]


def _setup_block(school):
    from mainApp import platform_services

    items = platform_services.setup_checklist(school)
    done = sum(1 for i in items if i["done"])
    return {"items": items, "done": done, "total": len(items), "pct": _pct(done, len(items)),
            "complete": done == len(items)}


def _sms_account(school):
    from mainApp.models.platform_models import SchoolMessagingAccount
    return SchoolMessagingAccount.objects.filter(school=school).first()


# =============================================================================
# THE WHOLE PLATFORM (Muzadi)
# =============================================================================
def platform_summary():
    """Rows, alerts and totals for every school (shared by /platform/ and the home dashboard)."""
    from mainApp import platform_services, tenancy

    rows = platform_services.schools_overview()
    usage = platform_services.sms_usage_this_month()
    for r in rows:
        r["sms_this_month"] = usage.get(r["school"].id, 0)
        r["url"] = (tenancy.school_url(r["school"], "/")
                    if r["school"].subdomain or r["school"].custom_domain else "")
    alerts = {
        "expiring": [r for r in rows if r["days_left"] is not None and 0 <= r["days_left"] <= 14
                     and r["state"] in ("ACTIVE", "TRIAL")],
        "grace": [r for r in rows if r["state"] == "GRACE"],
        "read_only": [r for r in rows if r["state"] == "READ_ONLY"],
        "low_sms": [r for r in rows if r["messaging"] and r["messaging"].sms_enabled and r["messaging"].is_low],
        "over_limit": [r for r in rows if r["over_limit"]],
        "no_address": [r for r in rows if not (r["school"].subdomain or r["school"].custom_domain)],
    }
    totals = {
        "schools": len(rows),
        "active": sum(1 for r in rows if r["school"].is_active and r["state"] in ("ACTIVE", "TRIAL", "GRACE")),
        "students": sum(r["students"] for r in rows),
        "staff": sum(r["staff"] for r in rows),
        "sms": sum(r["sms_this_month"] for r in rows),
    }
    totals["attention"] = len(alerts["expiring"]) + len(alerts["grace"]) + len(alerts["read_only"])
    return rows, alerts, totals


def platform_dashboard(today=None):
    from mainApp import tenancy

    today = today or timezone.localdate()
    with tenancy.guard_disabled():
        rows, alerts, totals = platform_summary()
        data = {"rows": rows, "alerts": alerts, "totals": totals}
        data["alert_count"] = sum(len(v) for v in alerts.values())
        data.update(_safe(lambda: _platform_states(rows), {}))
        data.update(_safe(lambda: _platform_money(today), {}))
        data.update(_safe(lambda: _platform_sms(today), {}))
        data.update(_safe(lambda: _platform_growth(today), {}))
        data.update(_safe(lambda: _platform_users(today), {}))
        data["top_schools"] = sorted(rows, key=lambda r: r["students"], reverse=True)[:8]
        data["chart_top_schools"] = {
            "labels": [r["school"].short_name or r["school"].name for r in data["top_schools"]],
            "values": [r["students"] for r in data["top_schools"]],
        }
        data["recent_activity"] = _safe(_platform_activity, [])
        data["release_notes"] = _safe(_platform_release_notes, [])
        data["base_domain"] = _safe(tenancy.base_domain, "")
    return data


def _platform_states(rows):
    order = [("ACTIVE", "Active"), ("TRIAL", "Trial"), ("GRACE", "Grace period"),
             ("READ_ONLY", "Read-only"), ("SUSPENDED", "Suspended")]
    counts = {code: 0 for code, _ in order}
    plans = {}
    for r in rows:
        counts[r["state"]] = counts.get(r["state"], 0) + 1
        sub = r["subscription"]
        plan = sub.get_plan_display() if sub else "No plan"
        plans[plan] = plans.get(plan, 0) + 1
    return {
        "state_rows": [{"code": c, "label": l, "count": counts.get(c, 0), "pct": _pct(counts.get(c, 0), len(rows))}
                       for c, l in order],
        "plan_rows": [{"label": k, "count": v, "pct": _pct(v, len(rows))}
                      for k, v in sorted(plans.items(), key=lambda kv: -kv[1])],
        "chart_states": {"labels": [l for _, l in order], "values": [counts.get(c, 0) for c, _ in order]},
    }


def _platform_money(today):
    from mainApp.models.platform_models import SchoolSubscription, SmsCreditEntry, SubscriptionPayment

    months = _month_starts(today, 12)
    subs = {_month_key(r["m"]): _num(r["t"]) for r in
            SubscriptionPayment.objects.filter(paid_on__gte=months[0]).order_by().annotate(m=TruncMonth("paid_on"))
            .values("m").annotate(t=Sum("amount_ugx")).values("m", "t") if r["m"]}
    sms = {_month_key(r["m"]): _num(r["t"]) for r in
           SmsCreditEntry.objects.filter(kind=SmsCreditEntry.KIND_TOPUP, created_at__date__gte=months[0])
           .order_by().annotate(m=TruncMonth("created_at")).values("m").annotate(t=Sum("amount_ugx")).values("m", "t") if r["m"]}

    sub_series = [subs.get((m.year, m.month), 0) for m in months]
    sms_series = [sms.get((m.year, m.month), 0) for m in months]
    year_start = today.replace(month=1, day=1)
    revenue_year = _num(SubscriptionPayment.objects.filter(paid_on__gte=year_start).aggregate(t=Sum("amount_ugx"))["t"]) \
        + _num(SmsCreditEntry.objects.filter(kind=SmsCreditEntry.KIND_TOPUP, created_at__date__gte=year_start)
               .aggregate(t=Sum("amount_ugx"))["t"])
    this_month = sub_series[-1] + sms_series[-1]
    last_month = sub_series[-2] + sms_series[-2]

    expected_term = _num(SchoolSubscription.objects.filter(status=SchoolSubscription.STATUS_ACTIVE)
                         .aggregate(t=Sum("price_per_term_ugx"))["t"])

    return {
        "revenue_year": revenue_year,
        "revenue_month": this_month,
        "revenue_change": (_pct(this_month - last_month, last_month) if last_month else None),
        "expected_term_revenue": expected_term,
        "recent_payments": list(SubscriptionPayment.objects.select_related("school").order_by("-paid_on", "-id")[:6]),
        "recent_topups": list(SmsCreditEntry.objects.filter(kind=SmsCreditEntry.KIND_TOPUP)
                              .select_related("school").order_by("-created_at")[:6]),
        "chart_revenue": {"labels": [m.strftime("%b %y") for m in months],
                          "subscriptions": sub_series, "sms": sms_series},
    }


def _platform_sms(today):
    from mainApp.models.platform_models import SchoolMessagingAccount, SmsCreditEntry

    months = _month_starts(today, 6)
    used = {_month_key(r["m"]): -_num(r["t"]) for r in
            SmsCreditEntry.objects.filter(kind__in=[SmsCreditEntry.KIND_USAGE, SmsCreditEntry.KIND_REFUND],
                                          created_at__date__gte=months[0])
            .order_by().annotate(m=TruncMonth("created_at")).values("m").annotate(t=Sum("units")).values("m", "t") if r["m"]}
    accounts = SchoolMessagingAccount.objects.all()
    return {
        "sms_credit_total": _num(accounts.filter(billing_mode=SchoolMessagingAccount.BILLING_PREPAID)
                                 .aggregate(t=Sum("credit_units"))["t"]),
        "sms_enabled_schools": accounts.filter(sms_enabled=True).count(),
        "whatsapp_schools": accounts.filter(whatsapp_enabled=True).count(),
        "chart_sms": {"labels": [m.strftime("%b") for m in months],
                      "values": [max(used.get((m.year, m.month), 0), 0) for m in months]},
    }


def _platform_growth(today):
    from mainApp.models.models import School
    from mainApp.models.student import Student

    months = _month_starts(today, 12)
    new_schools = {_month_key(r["m"]): r["n"] for r in
                   School._base_manager.filter(created_at__date__gte=months[0]).order_by().annotate(m=TruncMonth("created_at"))
                   .values("m").annotate(n=Count("id")).values("m", "n") if r["m"]}
    levels = list(Student._base_manager.filter(is_active=True).order_by().values("level__code", "level__name")
                  .annotate(n=Count("id")).order_by("level__code"))
    total = sum(r["n"] for r in levels)
    return {
        "schools_new_month": new_schools.get((today.year, today.month), 0),
        "students_new_month": Student._base_manager.filter(enrolled_date__gte=today.replace(day=1)).count(),
        "chart_new_schools": {"labels": [m.strftime("%b") for m in months],
                              "values": [new_schools.get((m.year, m.month), 0) for m in months]},
        "platform_level_rows": [{"label": r["level__code"] or r["level__name"] or "Unknown", "count": r["n"],
                                 "pct": _pct(r["n"], total)} for r in levels],
    }


def _platform_users(today):
    from django.contrib.auth.models import User

    users = User.objects.filter(is_active=True)
    return {
        "users_total": users.count(),
        "users_today": users.filter(last_login__date=today).count(),
        "users_week": users.filter(last_login__date__gte=today - timedelta(days=7)).count(),
    }


def _platform_activity():
    from mainApp.models.auditModels import AuditLog
    return list(AuditLog.objects.select_related("user").order_by("-timestamp")[:8])


def _platform_release_notes():
    from mainApp.models.platform_models import ReleaseNote
    return list(ReleaseNote.objects.order_by("-created_at")[:3])

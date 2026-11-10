"""
"Do this first" help for school users.

Some records depend on others: a student needs a class and a stream, a fee
structure needs fee categories and a term, marks need subject allocations,
and so on. This module keeps that knowledge in ONE place and is used by:

  * the yellow "Before you can ... first set up" box shown at the top of a
    page when something it depends on is still missing
    (context processor ``setup_prereq_context`` -> templates/platform/_prereqs.html)
  * the Setup guide page (school/setup/) and the setup checklist on the
    dashboard (platform_services.setup_checklist)

To add help for a new page: add its URL name to PAGE_NEEDS with the list of
requirement keys from REQUIREMENTS. To add a new kind of requirement: add an
entry to REQUIREMENTS with a check function.
"""
import logging

from django.urls import NoReverseMatch, reverse

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Checks: each takes a School and returns True when the data is in place.
# ---------------------------------------------------------------------------
def _school_details(school):
    return bool(school.logo and school.contact_phone)


def _academic_year(school):
    return bool(school.academic_year_id)


def _term(school):
    return bool(school.current_term_id)


def _classes(school):
    from mainApp.models.models import Class
    return Class.objects.filter(school=school).exists()


def _streams(school):
    from mainApp.models.models import Stream
    return Stream.objects.filter(class_ref__school=school).exists()


def _subjects(school):
    from mainApp.models.subject_models import Subject
    return Subject.objects.filter(level__in=school.level.all()).exists()


def _teachers(school):
    from mainApp.models.teacher_models import Teacher
    return Teacher.objects.filter(user__profile__school=school).exists()


def _students(school):
    from mainApp.models.student import Student
    return Student.objects.filter(school=school).exists()


def _parents(school):
    from mainApp.models.student import Parent
    return Parent.objects.filter(school=school).exists()


def _allocations(school):
    from mainApp.models.teacher_models import TeacherTeachingAllocation
    return TeacherTeachingAllocation.all_years.filter(class_ref__school=school).exists()


def _exam_setup(school):
    from mainApp.models.exams_models import ExamType
    return ExamType.objects.filter(scheme__school=school).exists()


def _grading(school):
    from mainApp.models.ple_models import PLEGradingScale
    from mainApp.models.uace_new_curiculum_models import NewUACEGradingScale
    from mainApp.models.uace_old_curiculum_models import UACEGradingScale
    from mainApp.models.uce_grading_models import GradingScale
    return any(m.objects.filter(school=school).exists()
               for m in (GradingScale, PLEGradingScale, NewUACEGradingScale, UACEGradingScale))


def _report_template(school):
    from mainApp.models.report_template_models import ReportCardTemplate
    return ReportCardTemplate.objects.filter(school=school).exists()


def _fee_categories(school):
    from mainApp.models.fees_models import FeeCategory
    return FeeCategory.objects.filter(school=school).exists()


def _fee_structure(school):
    from mainApp.models.fees_models import FeeStructure
    return FeeStructure.objects.filter(school=school).exists()


def _bell_schedule(school):
    from mainApp.models.timetable_models import TimetablePeriod
    return TimetablePeriod.objects.filter(school=school).exists()


def _hostels(school):
    from mainApp.models.student import Hostel
    return Hostel.objects.filter(school=school).exists()


def _hostel_rooms(school):
    from mainApp.models.hostel_models import HostelRoom
    return HostelRoom.objects.filter(school=school).exists()


def _hostel_beds(school):
    from mainApp.models.hostel_models import HostelBed
    return HostelBed.objects.filter(school=school).exists()


def _vehicles(school):
    from mainApp.models.transport_models import TransportVehicle
    return TransportVehicle.objects.filter(school=school).exists()


def _routes(school):
    from mainApp.models.transport_models import TransportRoute
    return TransportRoute.objects.filter(school=school).exists()


def _portal_users(school):
    from mainApp.models.models import Profile
    return Profile.all_objects.filter(school=school, role__in=("STUDENT", "PARENT")).exists()


def _sms_credit(school):
    from mainApp.models.platform_models import SchoolMessagingAccount
    acc = SchoolMessagingAccount.objects.filter(school=school).first()
    if acc is None or not acc.sms_enabled:
        return False
    return (not acc.is_prepaid) or acc.credit_units > 0


ADMIN_ROLES = ("ADMIN", "SUPER_ADMIN")
_FEE_ROLES = ADMIN_ROLES + ("BURSAR",)
_TRANSPORT_ROLES = ADMIN_ROLES + ("TRANSPORT_OFFICER",)
_PLATFORM_ROLES = ("SUPER_ADMIN",)
_MUZADI = "Muzadi Technologies"
_ADMIN = "the school administrator"


# ---------------------------------------------------------------------------
# Requirements: key -> what it is, how to check it, where to set it up.
#   roles: who can set it up (default: school admin). Everybody else is told
#          to ask the owner instead of getting a link they can't open.
#   owner: who to ask (default: the school administrator).
# ---------------------------------------------------------------------------
REQUIREMENTS = {
    "school_details": {"label": "School details and logo", "check": _school_details,
                       "url": "platform_dashboard", "roles": _PLATFORM_ROLES, "owner": _MUZADI,
                       "hint": "Send your logo and contact phone to Muzadi Technologies."},
    "academic_year": {"label": "Current academic year", "check": _academic_year,
                      "url": "academic_year_list", "roles": _PLATFORM_ROLES, "owner": _MUZADI,
                      "hint": "Academic years are shared by all schools and set by Muzadi Technologies."},
    "term": {"label": "Current term", "check": _term, "url": "academic_year_list",
             "roles": _PLATFORM_ROLES, "owner": _MUZADI,
             "hint": "Terms are shared by all schools and set by Muzadi Technologies."},
    "classes": {"label": "Classes", "check": _classes, "url": "class_list",
                "hint": "e.g. S.1 - S.6 or P.1 - P.7."},
    "streams": {"label": "Streams", "check": _streams, "url": "stream_list",
                "hint": "Every class needs at least one stream, e.g. S.1 East (one stream per class is fine)."},
    "subjects": {"label": "Subjects", "check": _subjects, "url": "subject_list",
                 "hint": "The subjects (and papers) taught at your level."},
    "teachers": {"label": "Teachers", "check": _teachers, "url": "teacher_list",
                 "hint": "Import them from Excel or add them one by one."},
    "students": {"label": "Students", "check": _students, "url": "student_list",
                 "hint": "Upload your Excel list or enrol them one by one."},
    "parents": {"label": "Parents and guardians", "check": _parents, "url": "parent_list",
                "hint": "Usually added while enrolling a student."},
    "allocations": {"label": "Subject allocation (who teaches what)", "check": _allocations,
                    "url": "teacher_allocation_list",
                    "hint": "Tell the system which teacher teaches which subject in which class and stream."},
    "exam_setup": {"label": "Exam setup and exam types", "check": _exam_setup, "url": "exam_setup_list",
                   "hint": "e.g. Beginning of Term, Mid Term, End of Term, with their weights."},
    "grading": {"label": "Grading scale", "check": _grading, "url": "uce_grading_scale_list",
                "hint": "How marks turn into grades, divisions or points."},
    "report_template": {"label": "Report card template", "check": _report_template,
                        "url": "report_template_list", "hint": "The layout of your report cards."},
    "fee_categories": {"label": "Fee categories", "check": _fee_categories, "roles": _FEE_ROLES, "url": "fee_category_list",
                       "hint": "e.g. Tuition, Development, Lunch."},
    "fee_structure": {"label": "Fee structure", "check": _fee_structure, "roles": _FEE_ROLES, "url": "fee_structure_list",
                      "hint": "How much each class pays in a term."},
    "bell_schedule": {"label": "Timetable periods (bell times)", "check": _bell_schedule,
                      "url": "timetable_period_list", "hint": "When each lesson starts and ends."},
    "hostels": {"label": "Hostels / dormitories", "check": _hostels, "url": "hostel_list", "hint": ""},
    "hostel_rooms": {"label": "Hostel rooms", "check": _hostel_rooms, "url": "hostel_room_list", "hint": ""},
    "hostel_beds": {"label": "Beds", "check": _hostel_beds, "url": "hostel_bed_list",
                    "hint": "A room can generate its beds for you."},
    "vehicles": {"label": "Vehicles", "check": _vehicles, "roles": _TRANSPORT_ROLES, "url": "transport_vehicle_list", "hint": ""},
    "routes": {"label": "Routes and stops", "check": _routes, "roles": _TRANSPORT_ROLES, "url": "transport_route_list", "hint": ""},
    "portal_users": {"label": "Student & parent logins", "check": _portal_users, "url": "user_management",
                     "hint": "Generate accounts and print the login slips."},
    "sms_credit": {"label": "SMS credit", "check": _sms_credit, "url": "school_sms_credit",
                   "hint": "Contact Muzadi Technologies to switch on SMS and buy credit."},
}


# ---------------------------------------------------------------------------
# Page (URL name) -> (what the page does, requirement keys in the order to do them)
# ---------------------------------------------------------------------------
_STUDENT_NEEDS = ["academic_year", "classes", "streams"]
_MARKS_NEEDS = ["term", "students", "allocations", "exam_setup"]

PAGE_NEEDS = {
    # Classes / streams
    "stream_create": ("add a stream", ["classes"]),
    # Students
    "enroll_student_step1": ("add a student", _STUDENT_NEEDS),
    "enroll_student_step2": ("add a student", _STUDENT_NEEDS),
    "student_bulk_upload": ("upload students", _STUDENT_NEEDS),
    "student_list": ("manage students", _STUDENT_NEEDS),
    # Parents
    "parent_link_students": ("link a parent to students", ["students"]),
    "account_candidates": ("create student and parent logins", ["students"]),
    # Calendar
    "term_add": ("add a term", ["academic_year"]),
    # Teaching
    "teacher_allocation_create": ("allocate subjects to teachers", ["classes", "streams", "subjects", "teachers"]),
    "teacher_allocation_list": ("allocate subjects to teachers", ["classes", "streams", "subjects", "teachers"]),
    "allocation_import_upload": ("import subject allocations", ["classes", "streams", "subjects", "teachers"]),
    "class_teacher_create": ("assign a class teacher", ["classes", "streams", "teachers"]),
    # Timetable
    "timetable_create": ("make a timetable", ["classes", "streams", "subjects", "teachers", "allocations", "bell_schedule"]),
    "timetable_entry_create": ("add a lesson to the timetable", ["classes", "streams", "allocations", "bell_schedule"]),
    # Exams / marks / reports
    "exam_type_create": ("add an exam type", ["exam_setup"]),
    "marks_entry": ("enter marks", _MARKS_NEEDS),
    "marks_import": ("import marks", _MARKS_NEEDS),
    "offline_marks_app": ("enter marks offline", _MARKS_NEEDS),
    "marks_progress": ("track marks entry", _MARKS_NEEDS),
    "competency_rows_entry": ("enter competency scores", ["term", "students", "allocations"]),
    "report_command_center": ("generate report cards", ["term", "students", "allocations", "exam_setup", "grading", "report_template"]),
    # Attendance
    "attendance_take": ("take attendance", ["classes", "streams", "students"]),
    "attendance_qr_take": ("take attendance", ["classes", "streams", "students"]),
    # Promotion
    "promotion_setup": ("set up promotion", ["academic_year", "classes", "streams"]),
    "promotion_dashboard": ("promote students", ["academic_year", "term", "classes", "streams", "students"]),
    # Fees
    "fee_structure_create": ("make a fee structure", ["academic_year", "term", "classes", "fee_categories"]),
    "fee_opening_balance_create": ("add an opening balance", ["students"]),
    "student_manual_charge_create": ("charge a student", ["students", "fee_categories"]),
    "bulk_manual_charge_create": ("charge students", ["students", "fee_categories"]),
    "fees_collect": ("collect fees", ["students", "fee_structure"]),
    # Communication
    "sms_compose": ("send SMS", ["students", "parents", "sms_credit"]),
    # Boarding
    "hostel_room_create": ("add a room", ["hostels"]),
    "hostel_bed_create": ("add a bed", ["hostels", "hostel_rooms"]),
    "hostel_assignment_create": ("give a student a bed", ["students", "hostels", "hostel_rooms", "hostel_beds"]),
    "hostel_auto_allocate": ("allocate beds", ["students", "hostels", "hostel_rooms", "hostel_beds"]),
    "boarding_rollcall_start": ("take roll call", ["students", "hostels"]),
    # Transport
    "transport_route_add": ("add a route", ["vehicles"]),
    "transport_assign": ("put students on transport", ["students", "vehicles", "routes"]),
    "transport_trip_start": ("start a trip", ["vehicles", "routes"]),
    # Health
    "health_sickbay_add": ("admit a student to sick bay", ["students"]),
}


# ---------------------------------------------------------------------------
# The full guide, in the order a new school should work.
# ---------------------------------------------------------------------------
GUIDE = [
    {"title": "1. School foundation", "intro": "Do these first. Almost everything else depends on them.",
     "steps": [
         ("school_details", "Your school's name, logo and contacts appear on report cards, receipts and SMS. Muzadi Technologies sets these up for you.", []),
         ("academic_year", "Academic years are shared by all schools. Muzadi Technologies sets the current year for your school.", []),
         ("term", "Terms (dates of Term 1, 2 and 3) are shared too. Muzadi Technologies marks the current term.", ["academic_year"]),
         ("classes", "Classes > Add class. One per class you teach (S.1, S.2 ...). Set the next class for promotion.", []),
         ("streams", "Streams > Add stream. Each class needs at least one stream, even if the class is not divided.", ["classes"]),
         ("subjects", "Subjects > Add subject. Add the papers for subjects that have more than one paper.", []),
     ]},
    {"title": "2. People", "intro": "Add the people once the classes exist.",
     "steps": [
         ("teachers", "Teachers > Import from Excel, or enrol one by one. Each teacher gets a login.", []),
         ("students", "Students > Bulk upload (Excel) or Enrol student. Choose the class and stream for each student.", ["academic_year", "classes", "streams"]),
         ("parents", "Parents are usually added while enrolling a student, or later under Parents and Guardians.", ["students"]),
         ("portal_users", "Users > Account candidates. Generate student and parent logins and print the slips.", ["students"]),
     ]},
    {"title": "3. Teaching", "intro": "Tell the system who teaches what. Marks entry and the timetable use this.",
     "steps": [
         ("allocations", "Subject allocation > Add (or import). Teacher + subject + class + stream.", ["classes", "streams", "subjects", "teachers"]),
         ("bell_schedule", "Timetable > Periods. Add the bell times, then build the timetable.", []),
     ]},
    {"title": "4. Exams and report cards", "intro": "Set these up before teachers start entering marks.",
     "steps": [
         ("exam_setup", "Exams > Exam setup. Add the exam types for the term and their weights.", []),
         ("grading", "Grading > choose your level (PLE, UCE or UACE) and set up the grading scale.", []),
         ("report_template", "Report cards > Templates. Choose what appears on the report card and add signatures.", []),
     ]},
    {"title": "5. Fees", "intro": "For the bursar. Students must exist before charges can be generated.",
     "steps": [
         ("fee_categories", "Fees > Fee categories. e.g. Tuition, Development, Lunch.", []),
         ("fee_structure", "Fees > Fee structure. Amount per class per term, then click Generate charges.", ["academic_year", "term", "classes", "fee_categories"]),
     ]},
    {"title": "6. Optional modules", "intro": "Only if your school uses them.",
     "steps": [
         ("sms_credit", "SMS to parents needs SMS switched on and credit. Parents need phone numbers.", ["parents"]),
         ("hostels", "Boarding > Hostels, then Rooms, then Beds, then give students beds.", []),
         ("hostel_rooms", "Boarding > Rooms (inside a hostel).", ["hostels"]),
         ("hostel_beds", "Boarding > Beds, or let a room generate its beds.", ["hostel_rooms"]),
         ("vehicles", "Transport > Vehicles, then Routes and stops, then assign students.", []),
         ("routes", "Transport > Routes. Add the stops and the termly charge.", ["vehicles"]),
     ]},
]

# What happens every term once the school is set up.
TERM_ROUTINE = [
    ("At the start of term", "Mark the new term as current; generate fee charges; check subject allocations for new teachers."),
    ("During the term", "Take attendance daily; collect fees; teachers enter marks for each exam type."),
    ("End of term", "Close marks entry; generate report cards; release them to parents (fees permitting)."),
    ("End of year (final term)", "Run Promotion to move students to the next class, then set up the new academic year."),
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _url(name):
    try:
        return reverse(name)
    except NoReverseMatch:
        return ""


def is_done(key, school, cache=None):
    """True when the requirement is in place. A failing check counts as done,
    so a bug here never blocks or nags a school."""
    if cache is not None and key in cache:
        return cache[key]
    try:
        ok = bool(REQUIREMENTS[key]["check"](school))
    except Exception:
        logger.exception("setup_guide check %s failed", key)
        ok = True
    if cache is not None:
        cache[key] = ok
    return ok


def can_fix(key, role):
    """Can this role open the page that sets the requirement up?"""
    return role in REQUIREMENTS[key].get("roles", ADMIN_ROLES)


def _item(key, school, cache, role):
    req = REQUIREMENTS[key]
    fix = can_fix(key, role)
    return {"key": key, "label": req["label"], "hint": req["hint"], "can_fix": fix,
            "url": _url(req["url"]) if fix else "", "ask": "" if fix else req.get("owner", _ADMIN),
            "done": is_done(key, school, cache)}


def missing_for_page(url_name, school, role, cache=None):
    """None when the page has no prerequisites or all are in place,
    else {"action": ..., "missing": [items]}."""
    entry = PAGE_NEEDS.get(url_name)
    if not entry or school is None:
        return None
    action, keys = entry
    cache = {} if cache is None else cache
    missing = [i for i in (_item(k, school, cache, role) for k in keys) if not i["done"]]
    if not missing:
        return None
    return {"action": action, "missing": missing}


def guide_for(school, role):
    """The full guide with live status for this school."""
    cache = {}
    phases, total, done = [], 0, 0
    for phase in GUIDE:
        steps = []
        for key, how, needs in phase["steps"]:
            item = _item(key, school, cache, role)
            item["how"] = how
            item["needs"] = [REQUIREMENTS[n]["label"] for n in needs if not is_done(n, school, cache)]
            item["blocked"] = bool(item["needs"]) and not item["done"]
            steps.append(item)
            total += 1
            done += item["done"]
        phases.append({"title": phase["title"], "intro": phase["intro"], "steps": steps})
    return {"phases": phases, "total": total, "done": done,
            "percent": int(done * 100 / total) if total else 100}

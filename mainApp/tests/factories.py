"""
Small helpers that build a realistic school for tests:
one school, one PLE level, one academic year with Term 1, one class,
students and fee charges.
"""
import itertools
from datetime import date
from decimal import Decimal

from django.contrib.auth.models import User

from mainApp.models.calendar import AcademicYear, Term
from mainApp.models.fees_models import FeeCategory, StudentFeeCharge
from mainApp.models.models import Class, EducationLevel, Profile, School
from mainApp.models.student import Student

_seq = itertools.count(1)


def make_school(name=None):
    n = next(_seq)
    level, _ = EducationLevel.objects.get_or_create(
        code="PLE", defaults={"name": "Primary Leaving Examination", "required_subjects": 4}
    )
    school = School.objects.create(
        school_id=f"SCH{n:04d}", name=name or f"Test School {n}", short_name=f"TS{n}"
    )
    school.level.add(level)
    # Academic years are shared by all schools (AcademicYear.year is unique).
    year, _ = AcademicYear.objects.get_or_create(
        year="2026", defaults={"start_date": date(2026, 2, 1), "end_date": date(2026, 12, 1)}
    )
    term, _ = Term.objects.get_or_create(
        academic_year=year, name="Term 1",
        defaults={"start_date": date(2026, 2, 1), "end_date": date(2026, 5, 1)},
    )
    klass = Class.objects.create(school=school, level=level, name=f"P.7-{n}")
    return {"school": school, "level": level, "year": year, "term": term, "class": klass}


def make_student(ctx, first="Amina", last=None):
    n = next(_seq)
    return Student.objects.create(
        candidate_id=f"C{n:05d}",
        school=ctx["school"],
        level=ctx["level"],
        current_class=ctx["class"],
        first_name=first,
        last_name=last or f"Nakato{n}",
        date_of_birth=date(2013, 1, 1),
        emergency_contact_name="Parent",
        emergency_contact_phone="0700000000",
        emergency_contact_relationship="Mother",
    )


def make_charge(ctx, student, amount, category_name="Tuition", priority=1):
    cat, _ = FeeCategory.objects.get_or_create(
        school=ctx["school"], name=category_name, defaults={"priority": priority}
    )
    charge = StudentFeeCharge.objects.create(
        school=ctx["school"], student=student,
        academic_year=ctx["year"], term=ctx["term"],
        fee_category=cat, amount=Decimal(str(amount)),
    )
    charge.refresh_status()
    charge.save(update_fields=["status"])
    return charge


def make_user(username="bursar", role="ADMIN", school=None, email="bursar@example.com",
              password="Str0ng-Pass!", verified=True):
    user = User.objects.create_user(username=username, password=password, email=email)
    Profile.objects.create(user=user, role=role, school=school, email_verified=verified)
    return user

"""Seed the local TEST database with fake data for browser + load testing.
Run: python manage.py shell < seed_demo.py"""
from datetime import date
from decimal import Decimal
from django.contrib.auth.models import User
from mainApp.models.models import Profile, Stream
from mainApp.models.student import Parent, StudentGuardian
from mainApp.tests.factories import make_school, make_student, make_charge
from mainApp import tenancy

with tenancy.guard_disabled():
    a = make_school("Alpha Primary School")
    s = a["school"]; s.subdomain = "alpha"; s.current_term = a["term"]; s.save()
    Stream.objects.create(class_ref=a["class"], name="East")

    def mk(username, role, school=s):
        u = User.objects.create_user(username, password="Str0ng-Pass!", email=f"{username}@example.com",
                                     first_name=username.title(), last_name="Test")
        Profile.objects.create(user=u, role=role, school=school, email_verified=True)
        return u

    mk("admin1", "ADMIN"); mk("bursar1", "BURSAR"); mk("super1", "SUPER_ADMIN", None)
    t = mk("teacher1", "TEACHER")
    from mainApp.models.teacher_models import Teacher
    Teacher.objects.create(user=t)

    N = 300
    for i in range(N):
        st = make_student(a, first=f"Pupil{i:03d}")
        make_charge(a, st, 350000)
        p = Parent.objects.create(school=s, first_name="Parent", last_name=f"{i:03d}",
                                  phone=f"07720{i:05d}", relationship="MOTHER", address="Kampala")
        if not StudentGuardian.objects.filter(student=st, parent=p).exists():
            st.parents.add(p)
        u = User.objects.create_user(f"parent{i:03d}", password="Str0ng-Pass!")
        Profile.objects.create(user=u, role="PARENT", school=s, email_verified=True)
        p.user = u; p.save()
    print("seeded", N, "students/parents")

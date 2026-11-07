"""Enter Marks filter: teachers only see their allocations, year/term follow the school setting."""
from datetime import date

from django.contrib.auth.models import User
from django.test import TestCase

from mainApp.academic_period import school_current_year_term
from mainApp.forms.forms_exams import MarksFilterForm
from mainApp.marks_filters import MarksFilterOptions
from mainApp.models.calendar import AcademicYear, Term
from mainApp.models.exams_models import ExamScheme, ExamType
from mainApp.models.models import Class, Profile, Stream
from mainApp.models.subject_models import Paper, Subject
from mainApp.models.teacher_models import Teacher, TeacherTeachingAllocation
from mainApp.tests.factories import make_school, make_student


def ids(rows):
    return [r["id"] for r in rows]


class MarksFilterTests(TestCase):
    def setUp(self):
        self.ctx = make_school("Filter Primary")
        self.school, self.klass, self.year, self.term1 = (self.ctx["school"], self.ctx["class"],
                                                          self.ctx["year"], self.ctx["term"])
        level = self.ctx["level"]
        self.term2 = Term.objects.create(academic_year=self.year, name="Term 2", order=2,
                                         start_date=date(2026, 6, 1), end_date=date(2026, 8, 1))
        # the school has already moved on to 2027 in its settings
        self.year27 = AcademicYear.objects.create(year="2027", start_date=date(2027, 2, 1),
                                                  end_date=date(2027, 12, 1), is_active=False)
        self.t27 = Term.objects.create(academic_year=self.year27, name="Term 1", order=1,
                                       start_date=date(2027, 2, 1), end_date=date(2027, 5, 1))
        self.school.academic_year = self.year27
        self.school.current_term = self.t27
        self.school.save()

        self.other_class = Class.objects.create(school=self.school, level=level, name="P.6-x")
        self.east = Stream.objects.create(class_ref=self.klass, name="East")
        self.west = Stream.objects.create(class_ref=self.klass, name="West")
        self.maths = Subject.objects.create(code="MTCF", short_name="MTC", name="Mathematics", level=level)
        self.english = Subject.objects.create(code="ENGF", short_name="ENG", name="English", level=level)
        self.science = Subject.objects.create(code="SCIF", short_name="SCI", name="Science", level=level)

        def exam(year, term, code):
            scheme = ExamScheme.objects.create(school=self.school, level=level, academic_year=year,
                                               term=term, name=f"{code} setup")
            return ExamType.objects.create(scheme=scheme, name=f"Exam {code}", code=code,
                                           applies_to="SUBJECT", weight_percent=100, max_mark=100)
        self.eot26 = exam(self.year, self.term1, "E26")
        self.eot27 = exam(self.year27, self.t27, "E27")

        self.user = User.objects.create_user(username="tnam", password="Str0ng-Pass!", first_name="Nam")
        Profile.objects.create(user=self.user, role="TEACHER", school=self.school, email_verified=True)
        self.teacher = Teacher.objects.create(user=self.user)
        # 2027: Maths in P.7 East only.  2026: English in the whole of P.7.
        TeacherTeachingAllocation.all_years.create(teacher=self.teacher, academic_year=self.year27,
                                                   class_ref=self.klass, stream=self.east, subject=self.maths)
        TeacherTeachingAllocation.all_years.create(teacher=self.teacher, academic_year=self.year,
                                                   class_ref=self.klass, stream=None, subject=self.english)

        self.admin = User.objects.create_user(username="adm", password="Str0ng-Pass!")
        Profile.objects.create(user=self.admin, role="ADMIN", school=self.school, email_verified=True)

        st = make_student(self.ctx, first="Grace")
        st.stream = self.east
        st.save()

    # ------------------------------------------------------------------ year / term defaults
    def test_school_setting_decides_current_year_and_term(self):
        self.assertEqual(school_current_year_term(self.school), (self.year27, self.t27))

    def test_opens_on_school_year_and_term(self):
        o = MarksFilterOptions(self.user, self.school, {}, defaults=True)
        self.assertEqual(o.selected["academic_year"], str(self.year27.id))
        self.assertEqual(o.selected["term"], str(self.t27.id))
        self.assertIn("(current)", o.options["academic_year"][0]["label"])

    def test_terms_belong_to_selected_year(self):
        o = MarksFilterOptions(self.user, self.school, {"academic_year": str(self.year.id)})
        self.assertEqual(set(ids(o.options["term"])), {str(self.term1.id), str(self.term2.id)})

    # ------------------------------------------------------------------ teacher sees only allocations
    def test_teacher_sees_only_allocated_class_stream_subject(self):
        o = MarksFilterOptions(self.user, self.school, {"academic_year": str(self.year27.id)})
        self.assertEqual(ids(o.options["class_ref"]), [str(self.klass.id)])       # P.6 not shown
        self.assertEqual(o.selected["class_ref"], str(self.klass.id))               # single option: auto
        self.assertEqual(ids(o.options["stream"]), [str(self.east.id)])            # West not shown
        self.assertTrue(o.stream_required)
        self.assertEqual(o.selected["stream"], str(self.east.id))
        self.assertEqual(ids(o.options["subject"]), [str(self.maths.id)])          # English is 2026
        self.assertEqual(ids(o.options["exam_type"]), [str(self.eot27.id)])        # only this term's exams

    def test_allocations_follow_the_selected_year(self):
        o = MarksFilterOptions(self.user, self.school, {"academic_year": str(self.year.id),
                                                        "term": str(self.term1.id)})
        self.assertFalse(o.stream_required)                        # whole class in 2026
        self.assertEqual(set(ids(o.options["stream"])), {str(self.east.id), str(self.west.id)})
        self.assertEqual(ids(o.options["subject"]), [str(self.english.id)])
        self.assertEqual(ids(o.options["exam_type"]), [str(self.eot26.id)])

    def test_admin_sees_everything(self):
        o = MarksFilterOptions(self.admin, self.school, {"academic_year": str(self.year27.id),
                                                         "class_ref": str(self.klass.id)})
        self.assertEqual(len(o.options["class_ref"]), 2)
        self.assertEqual(len(o.options["stream"]), 2)
        self.assertEqual(len(o.options["subject"]), 3)

    # ------------------------------------------------------------------ the reported error
    def test_exam_type_of_another_term_is_dropped_not_crashing(self):
        """Old page offered exam types of every term -> "9 is not one of the available choices"."""
        o = MarksFilterOptions(self.user, self.school, {
            "academic_year": str(self.year27.id), "term": str(self.t27.id),
            "class_ref": str(self.klass.id), "exam_type": str(self.eot26.id)})
        self.assertEqual(o.selected["exam_type"], str(self.eot27.id))   # replaced by the only valid one

    def test_form_choices_match_dropdown_options(self):
        data = {"academic_year": str(self.year27.id), "term": str(self.t27.id), "class_ref": str(self.klass.id),
                "stream": str(self.east.id), "exam_type": str(self.eot27.id), "subject": str(self.maths.id)}
        form = MarksFilterForm(data=data, school=self.school, user=self.user)
        self.assertTrue(form.is_valid(), form.errors)

    def test_teacher_cannot_submit_unallocated_subject(self):
        data = {"academic_year": str(self.year27.id), "term": str(self.t27.id), "class_ref": str(self.klass.id),
                "stream": str(self.east.id), "exam_type": str(self.eot27.id), "subject": str(self.science.id)}
        form = MarksFilterForm(data=data, school=self.school, user=self.user)
        self.assertFalse(form.is_valid())
        self.assertIn("subject", form.errors)

    def test_stream_required_when_teacher_has_only_some_streams(self):
        data = {"academic_year": str(self.year27.id), "term": str(self.t27.id), "class_ref": str(self.klass.id),
                "exam_type": str(self.eot27.id), "subject": str(self.maths.id)}
        form = MarksFilterForm(data=data, school=self.school, user=self.user)
        self.assertFalse(form.is_valid())
        self.assertIn("stream", form.errors)

    def test_paper_exam_types(self):
        p1 = Paper.objects.create(subject=self.maths, paper_code="MF1", paper_name="Paper 1", max_marks=100, order=1)
        Paper.objects.create(subject=self.maths, paper_code="MF2", paper_name="Paper 2", max_marks=100, order=2)
        self.eot27.applies_to = "PAPER"
        self.eot27.save()
        alloc = TeacherTeachingAllocation.all_years.get(teacher=self.teacher, subject=self.maths)
        alloc.papers.add(p1)
        o = MarksFilterOptions(self.user, self.school, {"academic_year": str(self.year27.id),
                                                        "exam_type": str(self.eot27.id)})
        self.assertEqual(o.exam_applies_to, "PAPER")
        self.assertEqual(ids(o.options["paper"]), [str(p1.id)])        # Paper 2 not allocated
        # old offline lists only carry the paper -> still accepted
        o2 = MarksFilterOptions(self.user, self.school, {
            "academic_year": str(self.year27.id), "term": str(self.t27.id), "class_ref": str(self.klass.id),
            "stream": str(self.east.id), "exam_type": str(self.eot27.id), "paper": str(p1.id)}, autoselect=False)
        self.assertEqual(o2.selected["paper"], str(p1.id))

    # ------------------------------------------------------------------ HTTP
    def test_options_endpoint_and_page(self):
        self.client.login(username="tnam", password="Str0ng-Pass!")
        data = self.client.get("/marks/filter-options/", {"defaults": "1"}).json()
        self.assertEqual(data["selected"]["academic_year"], str(self.year27.id))
        self.assertEqual(data["selected"]["subject"], str(self.maths.id))

        page = self.client.get("/marks/entry/")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "My mark lists")
        self.assertContains(page, f'value="{self.year27.id}" selected')

        params = {"academic_year": self.year27.id, "term": self.t27.id, "class_ref": self.klass.id,
                  "stream": self.east.id, "exam_type": self.eot27.id, "subject": self.maths.id}
        page = self.client.get("/marks/entry/", params)
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Grace")
        self.assertTrue(page.context["students"])

    def test_year_terms_endpoint(self):
        self.client.login(username="adm", password="Str0ng-Pass!")
        data = self.client.get("/ajax/year-terms/", {"year_id": self.year27.id}).json()
        self.assertEqual(data["current_term_id"], self.t27.id)
        self.assertEqual(data["current_year_id"], self.year27.id)
        self.assertEqual([r["id"] for r in data["results"]], [self.t27.id])

    def test_audit_page_uses_same_filter(self):
        self.client.login(username="tnam", password="Str0ng-Pass!")
        res = self.client.get("/marks/audit/")
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "/marks/filter-options/")
        params = {"academic_year": self.year27.id, "term": self.t27.id, "class_ref": self.klass.id,
                  "stream": self.east.id, "exam_type": self.eot27.id, "subject": self.maths.id}
        res = self.client.get("/marks/audit/", params)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.context["summary"]["total_students"], 1)

    def test_new_exam_setup_form_opens_on_school_year(self):
        from mainApp.forms.forms_exams import ExamSchemeForm
        form = ExamSchemeForm(school=self.school)
        self.assertEqual(form.initial["academic_year"], self.year27.pk)
        self.assertEqual(form.initial["term"], self.t27.pk)
        self.assertIn(self.year27, form.fields["academic_year"].queryset)
        self.assertEqual(list(form.fields["term"].queryset), [self.t27])

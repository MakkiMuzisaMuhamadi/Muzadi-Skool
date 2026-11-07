"""
Options for the "Enter Marks" filter (Year -> Term -> Class -> Stream -> Exam type -> Subject -> Paper).

The same code fills the dropdowns (AJAX) AND validates the submitted form, so a
value the teacher can pick is always a value the server accepts (no more
"Select a valid choice. 9 is not one of the available choices").

Teachers only see what is allocated to them for the selected academic year:
their classes, their streams, their subjects and their papers.
Administrators see everything in the school.

A selected value that is not available any more (e.g. the year was changed) is
dropped, so the lists below it are rebuilt from what is still valid.
When a list has exactly one option it is picked automatically.
"""
from django.db.models import Q

from mainApp.academic_period import current_term_for_year, school_current_year, terms_for_year
from mainApp.models.calendar import AcademicYear
from mainApp.models.exams_models import ExamScheme, ExamType
from mainApp.models.models import Class, Stream
from mainApp.models.subject_models import Paper, Subject
from mainApp.models.teacher_models import TeacherTeachingAllocation

FIELDS = ("academic_year", "term", "class_ref", "stream", "exam_type", "subject", "paper")


def is_teacher_user(user):
    role = getattr(getattr(user, "profile", None), "role", None)
    return role == "TEACHER" and hasattr(user, "teacher")


def teacher_allocations(user, school, year_id):
    """The teacher's allocations for one academic year (rows without a year count for every year)."""
    if not is_teacher_user(user) or not school:
        return TeacherTeachingAllocation.all_years.none()
    qs = TeacherTeachingAllocation.all_years.filter(teacher=user.teacher, class_ref__school=school)
    if year_id:
        qs = qs.filter(Q(academic_year_id=year_id) | Q(academic_year__isnull=True))
    return qs


def _clean_id(value):
    value = str(value or "").strip()
    return value if value.isdigit() else ""


class MarksFilterOptions:
    """
    opts = MarksFilterOptions(user, school, request.GET)
    opts.selected  -> {"academic_year": "12", "term": "40", ...} (only valid values)
    opts.options   -> {"academic_year": [{"id": "12", "label": "2026 (current)"}], ...}
    """

    def __init__(self, user, school, data=None, defaults=True, autoselect=True):
        self.user = user
        self.school = school
        self.teacher = is_teacher_user(user)
        self.autoselect = autoselect
        raw = {f: _clean_id((data or {}).get(f)) for f in FIELDS}
        self.selected = {f: "" for f in FIELDS}
        self.options = {f: [] for f in FIELDS}
        self.stream_required = False
        self.exam_applies_to = ""
        self.messages = {}
        if not school:
            return
        self._build(raw, defaults)

    # ------------------------------------------------------------------ helpers
    def _pick(self, field, rows, wanted, auto=None):
        """Store the options of a field and keep `wanted` only if it is one of them."""
        self.options[field] = rows
        ids = {r["id"] for r in rows}
        if wanted and wanted in ids:
            self.selected[field] = wanted
        elif (self.autoselect if auto is None else auto) and len(rows) == 1:
            self.selected[field] = rows[0]["id"]
        return self.selected[field]

    # ------------------------------------------------------------------ build
    def _build(self, raw, defaults):
        school = self.school
        current_year = school_current_year(school)

        # ---- Academic years
        year_ids = set(ExamScheme.objects.filter(school=school).values_list("academic_year_id", flat=True))
        if current_year:
            year_ids.add(current_year.id)
        if self.teacher:
            year_ids |= set(TeacherTeachingAllocation.all_years.filter(
                teacher=self.user.teacher, class_ref__school=school, academic_year__isnull=False,
            ).values_list("academic_year_id", flat=True))
        if raw["academic_year"]:
            year_ids.add(int(raw["academic_year"]))
        years = AcademicYear.objects.filter(id__in=year_ids).order_by("-start_date")
        cur_id = getattr(current_year, "id", None)
        rows = [{"id": str(y.id), "label": f"{y.year} (current)" if y.id == cur_id else str(y.year)} for y in years]
        wanted = raw["academic_year"] or (str(cur_id) if defaults and cur_id else "")
        year_id = self._pick("academic_year", rows, wanted)
        if not year_id:
            return

        # ---- Terms of that year
        current_term = current_term_for_year(school, int(year_id))
        cur_term_id = getattr(current_term, "id", None)
        rows = [{"id": str(t.id), "label": f"{t.name} (current)" if t.id == cur_term_id else t.name}
                for t in terms_for_year(year_id)]
        if not rows:
            self.messages["term"] = "No terms have been set up for this academic year."
        wanted = raw["term"] or (str(cur_term_id) if cur_term_id and (defaults or raw["academic_year"]) else "")
        term_id = self._pick("term", rows, wanted)

        # ---- Classes
        allocs = teacher_allocations(self.user, school, int(year_id)) if self.teacher else None
        if self.teacher:
            class_qs = Class.objects.filter(id__in=allocs.values("class_ref_id"))
            if not allocs.exists():
                self.messages["class_ref"] = "You have no classes allocated for this academic year. Ask the administrator."
        else:
            class_qs = Class.objects.filter(school=school)
        class_qs = class_qs.select_related("level").order_by("level__order", "name")
        rows = [{"id": str(c.id), "label": c.name} for c in class_qs]
        class_id = self._pick("class_ref", rows, raw["class_ref"])
        if not class_id:
            return
        klass = next(c for c in class_qs if str(c.id) == class_id)

        # ---- Streams
        class_streams = Stream.objects.filter(class_ref=klass).order_by("name")
        if self.teacher:
            class_allocs = allocs.filter(class_ref=klass)
            whole_class = class_allocs.filter(stream__isnull=True).exists()
            stream_qs = class_streams if whole_class else class_streams.filter(
                id__in=class_allocs.exclude(stream__isnull=True).values("stream_id"))
            # "All streams" is only possible when the teacher has the whole class.
            self.stream_required = not whole_class and stream_qs.exists()
        else:
            class_allocs = None
            stream_qs = class_streams
        rows = [{"id": str(s.id), "label": s.name} for s in stream_qs]
        stream_id = self._pick("stream", rows, raw["stream"], auto=self.stream_required)

        # ---- Exam types (this year + term + class level)
        exam_qs = ExamType.objects.none()
        if term_id and klass.level_id:
            exam_qs = (ExamType.objects.filter(
                scheme__school=school, scheme__level_id=klass.level_id,
                scheme__academic_year_id=year_id, scheme__term_id=term_id,
                scheme__is_active=True, is_active=True,
            ).exclude(source="AOI").select_related("scheme").order_by("order", "name"))
            if not exam_qs.exists():
                self.messages["exam_type"] = "No exam types are set up for this class level in this term."
        exam_list = list(exam_qs)
        rows = [{"id": str(e.id), "label": f"{e.name} ({e.code})" if e.code and e.code != e.name else e.name,
                 "applies_to": e.applies_to, "max_mark": str(e.max_mark)} for e in exam_list]
        exam_id = self._pick("exam_type", rows, raw["exam_type"])
        exam = next((e for e in exam_list if str(e.id) == exam_id), None)
        self.exam_applies_to = exam.applies_to if exam else ""

        # ---- Subjects
        if raw["paper"] and not raw["subject"]:
            # older links / offline lists only carry the paper: take its subject
            raw["subject"] = str(Paper.objects.filter(id=raw["paper"]).values_list("subject_id", flat=True).first() or "")
        if self.teacher:
            subj_allocs = class_allocs
            if stream_id:
                subj_allocs = subj_allocs.filter(Q(stream_id=stream_id) | Q(stream__isnull=True))
            else:
                subj_allocs = subj_allocs.filter(stream__isnull=True)
            subj_qs = Subject.objects.filter(id__in=subj_allocs.values("subject_id"))
            if self.exam_applies_to == "PAPER":
                subj_qs = subj_qs.filter(id__in=subj_allocs.filter(papers__isnull=False).values("subject_id"))
        else:
            subj_allocs = None
            subj_qs = Subject.objects.filter(level_id=klass.level_id) if klass.level_id else Subject.objects.none()
            if self.exam_applies_to == "PAPER":
                subj_qs = subj_qs.filter(papers__isnull=False)
        subj_qs = subj_qs.distinct().order_by("name")
        rows = [{"id": str(s.id), "label": s.name} for s in subj_qs]
        if self.teacher and not rows:
            if self.stream_required and not stream_id:
                self.messages["subject"] = "Choose your stream first."
            else:
                self.messages["subject"] = ("No subjects allocated to you here" +
                                            (" that have papers." if self.exam_applies_to == "PAPER" else "."))
        subject_id = self._pick("subject", rows, raw["subject"])

        # ---- Papers (only for exam types marked per paper)
        if self.exam_applies_to != "PAPER" or not subject_id:
            return
        if self.teacher:
            paper_ids = subj_allocs.filter(subject_id=subject_id).values_list("papers__id", flat=True)
            paper_qs = Paper.objects.filter(id__in=[p for p in paper_ids if p])
        else:
            paper_qs = Paper.objects.filter(subject_id=subject_id)
        paper_qs = paper_qs.distinct().order_by("order", "paper_name")
        rows = [{"id": str(p.id), "label": p.paper_name} for p in paper_qs]
        self._pick("paper", rows, raw["paper"])

    # ------------------------------------------------------------------ output
    def placeholder(self, field):
        if field == "stream" and self.stream_required:
            return "-- Select Stream --"
        return PLACEHOLDERS[field]

    def choices(self, field):
        return [("", self.placeholder(field))] + [(r["id"], r["label"]) for r in self.options[field]]

    def as_json(self):
        return {
            "selected": self.selected,
            "options": self.options,
            "stream_required": self.stream_required,
            "exam_applies_to": self.exam_applies_to,
            "messages": self.messages,
            "is_teacher": self.teacher,
            "placeholders": {f: self.placeholder(f) for f in FIELDS},
        }


PLACEHOLDERS = {
    "academic_year": "-- Select Academic Year --",
    "term": "-- Select Term --",
    "class_ref": "-- Select Class --",
    "stream": "-- All Streams --",
    "exam_type": "-- Select Exam Type --",
    "subject": "-- Select Subject --",
    "paper": "-- Select Paper --",
}


def teacher_mark_lists(user, school, year, term):
    """
    Every mark list a teacher has to fill for one term, built from their allocations:
    allocation (class/stream/subject or papers) x active exam types of that term.
    Returns [{"params": {...}, "title": "P.5 (East) • Mathematics", "exam": "EOT"}].
    """
    if not (is_teacher_user(user) and school and year and term):
        return []
    allocs = (teacher_allocations(user, school, year.id)
              .select_related("class_ref", "class_ref__level", "stream", "subject")
              .prefetch_related("papers")
              .order_by("class_ref__name", "stream__name", "subject__name"))

    exam_types_by_level = {}
    lists, seen = [], set()
    for alloc in allocs:
        level_id = alloc.class_ref.level_id
        if level_id not in exam_types_by_level:
            exam_types_by_level[level_id] = list(
                ExamType.objects.filter(
                    scheme__school=school, scheme__level_id=level_id,
                    scheme__academic_year=year, scheme__term=term,
                    scheme__is_active=True, is_active=True,
                ).exclude(source="AOI").order_by("order", "name")
            )
        for et in exam_types_by_level[level_id]:
            if et.applies_to == "PAPER":
                targets = [(None, p) for p in alloc.papers.all().order_by("order", "paper_name")]
            else:
                targets = [(alloc.subject, None)]
            for subject, paper in targets:
                params = {
                    "academic_year": str(year.id),
                    "term": str(term.id),
                    "class_ref": str(alloc.class_ref_id),
                    "exam_type": str(et.id),
                }
                if alloc.stream_id:
                    params["stream"] = str(alloc.stream_id)
                if paper:
                    params["paper"] = str(paper.id)
                    params["subject"] = str(alloc.subject_id)
                    target = f"{alloc.subject.name} - {paper.paper_name}"
                else:
                    params["subject"] = str(subject.id)
                    target = subject.name
                key = tuple(sorted(params.items()))
                if key in seen:
                    continue
                seen.add(key)
                cls = alloc.class_ref.name + (f" ({alloc.stream.name})" if alloc.stream_id else " (all streams)")
                lists.append({"params": params, "title": f"{cls} • {target}", "exam": et.code or et.name})
    lists.sort(key=lambda x: (x["title"], x["exam"]))
    return lists

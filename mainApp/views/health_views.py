# mainApp/views/health_views.py
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.utils import timezone
from django.views.generic import ListView, CreateView, UpdateView, DeleteView

from mainApp.models.models import Class, Stream
from mainApp.models.student import Student
from mainApp.models.health_models import SickBayVisit, MedicationRecord
from mainApp.forms.health_forms import StudentMedicalMiniForm, SickBayVisitForm, MedicationRecordForm


def get_school(request):
    return getattr(getattr(request.user, "profile", None), "school", None)


class SchoolScopedMixin:
    """
    Ensures school isolation.
    """
    def get_school(self):
        return get_school(self.request)

    def get_queryset(self):
        qs = super().get_queryset()
        school = self.get_school()
        return qs.filter(school=school) if school else qs.none()

    def form_valid(self, form):
        school = self.get_school()
        if school and hasattr(form.instance, "school_id") and not form.instance.school_id:
            form.instance.school = school
        return super().form_valid(form)

    def get_form_kwargs(self):
        kw = super().get_form_kwargs()
        kw["user"] = self.request.user
        return kw


# ---------------------------
# Medical Records (Student medical fields)
# ---------------------------
@login_required
def medical_records_list(request):
    school = get_school(request)
    if not school:
        return render(request, "dashboard/health/medical_records_list.html", {"students": []})

    class_id = request.GET.get("class")
    stream_id = request.GET.get("stream")
    q = (request.GET.get("q") or "").strip()

    students = Student.objects.filter(school=school, is_active=True).select_related("current_class", "stream")

    if class_id:
        students = students.filter(current_class_id=class_id)
    if stream_id:
        students = students.filter(stream_id=stream_id)
    if q:
        students = students.filter(
            Q(name__icontains=q) |
            Q(admission_number__icontains=q) |
            Q(first_name__icontains=q) |
            Q(last_name__icontains=q)
        )

    classes = Class.objects.filter(school=school).order_by("name")
    streams = Stream.objects.filter(class_ref_id=class_id).order_by("name") if class_id else Stream.objects.none()

    ctx = {
        "students": students.order_by("current_class__name", "stream__name", "name"),
        "classes": classes,
        "streams": streams,
        "filters": {"class": class_id or "", "stream": stream_id or "", "q": q},
    }
    return render(request, "dashboard/health/medical_records_list.html", ctx)


@login_required
def medical_record_update(request, pk: int):
    school = get_school(request)
    student = get_object_or_404(Student, pk=pk, school=school)

    if request.method == "POST":
        form = StudentMedicalMiniForm(request.POST, instance=student)
        if form.is_valid():
            form.save()
            return redirect("health_medical_records")
    else:
        form = StudentMedicalMiniForm(instance=student)

    return render(request, "dashboard/health/medical_record_form.html", {
        "student": student,
        "form": form,
        "title": "Update Medical Record",
        "action": "Save Changes",
    })


# ---------------------------
# Sick Bay
# ---------------------------
class SickBayVisitListView(LoginRequiredMixin, SchoolScopedMixin, ListView):
    model = SickBayVisit
    template_name = "dashboard/health/sickbay_list.html"
    context_object_name = "rows"
    paginate_by = 50

    def get_queryset(self):
        qs = super().get_queryset().select_related("student", "class_ref", "stream", "attended_by")
        class_id = self.request.GET.get("class")
        stream_id = self.request.GET.get("stream")
        q = (self.request.GET.get("q") or "").strip()
        date_from = self.request.GET.get("from")
        date_to = self.request.GET.get("to")

        if class_id:
            qs = qs.filter(class_ref_id=class_id)
        if stream_id:
            qs = qs.filter(stream_id=stream_id)
        if q:
            qs = qs.filter(
                Q(student__name__icontains=q) |
                Q(student__admission_number__icontains=q) |
                Q(symptoms__icontains=q) |
                Q(diagnosis__icontains=q)
            )
        if date_from:
            qs = qs.filter(visit_date__date__gte=date_from)
        if date_to:
            qs = qs.filter(visit_date__date__lte=date_to)

        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        school = self.get_school()
        class_id = self.request.GET.get("class")
        ctx["classes"] = Class.objects.filter(school=school).order_by("name") if school else Class.objects.none()
        ctx["streams"] = Stream.objects.filter(class_ref_id=class_id).order_by("name") if class_id else Stream.objects.none()
        ctx["filters"] = {
            "class": class_id or "",
            "stream": self.request.GET.get("stream") or "",
            "q": (self.request.GET.get("q") or "").strip(),
            "from": self.request.GET.get("from") or "",
            "to": self.request.GET.get("to") or "",
        }
        return ctx


class SickBayVisitCreateView(LoginRequiredMixin, SchoolScopedMixin, CreateView):
    model = SickBayVisit
    form_class = SickBayVisitForm
    template_name = "dashboard/health/sickbay_form.html"
    success_url = reverse_lazy("health_sickbay_list")

    def form_valid(self, form):
        # auto sync class/stream from student if not set
        obj = form.instance
        if obj.student_id:
            obj.class_ref = obj.class_ref or obj.student.current_class
            obj.stream = obj.stream or obj.student.stream
        obj.attended_by = self.request.user
        return super().form_valid(form)


class SickBayVisitUpdateView(LoginRequiredMixin, SchoolScopedMixin, UpdateView):
    model = SickBayVisit
    form_class = SickBayVisitForm
    template_name = "dashboard/health/sickbay_form.html"
    success_url = reverse_lazy("health_sickbay_list")

    def form_valid(self, form):
        obj = form.instance
        if obj.student_id:
            obj.class_ref = obj.class_ref or obj.student.current_class
            obj.stream = obj.stream or obj.student.stream
        return super().form_valid(form)


class SickBayVisitDeleteView(LoginRequiredMixin, SchoolScopedMixin, DeleteView):
    model = SickBayVisit
    template_name = "dashboard/health/confirm_delete.html"
    success_url = reverse_lazy("health_sickbay_list")


# ---------------------------
# Medication
# ---------------------------
class MedicationRecordListView(LoginRequiredMixin, SchoolScopedMixin, ListView):
    model = MedicationRecord
    template_name = "dashboard/health/medication_list.html"
    context_object_name = "rows"
    paginate_by = 50

    def get_queryset(self):
        qs = super().get_queryset().select_related("student", "class_ref", "stream", "administered_by")
        class_id = self.request.GET.get("class")
        stream_id = self.request.GET.get("stream")
        q = (self.request.GET.get("q") or "").strip()
        date_from = self.request.GET.get("from")
        date_to = self.request.GET.get("to")

        if class_id:
            qs = qs.filter(class_ref_id=class_id)
        if stream_id:
            qs = qs.filter(stream_id=stream_id)
        if q:
            qs = qs.filter(
                Q(student__name__icontains=q) |
                Q(student__admission_number__icontains=q) |
                Q(medicine_name__icontains=q) |
                Q(reason__icontains=q)
            )
        if date_from:
            qs = qs.filter(given_on__date__gte=date_from)
        if date_to:
            qs = qs.filter(given_on__date__lte=date_to)

        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        school = self.get_school()
        class_id = self.request.GET.get("class")
        ctx["classes"] = Class.objects.filter(school=school).order_by("name") if school else Class.objects.none()
        ctx["streams"] = Stream.objects.filter(class_ref_id=class_id).order_by("name") if class_id else Stream.objects.none()
        ctx["filters"] = {
            "class": class_id or "",
            "stream": self.request.GET.get("stream") or "",
            "q": (self.request.GET.get("q") or "").strip(),
            "from": self.request.GET.get("from") or "",
            "to": self.request.GET.get("to") or "",
        }
        return ctx


class MedicationRecordCreateView(LoginRequiredMixin, SchoolScopedMixin, CreateView):
    model = MedicationRecord
    form_class = MedicationRecordForm
    template_name = "dashboard/health/medication_form.html"
    success_url = reverse_lazy("health_medication_list")

    def form_valid(self, form):
        obj = form.instance
        if obj.student_id:
            obj.class_ref = obj.class_ref or obj.student.current_class
            obj.stream = obj.stream or obj.student.stream
        obj.administered_by = self.request.user
        return super().form_valid(form)


class MedicationRecordUpdateView(LoginRequiredMixin, SchoolScopedMixin, UpdateView):
    model = MedicationRecord
    form_class = MedicationRecordForm
    template_name = "dashboard/health/medication_form.html"
    success_url = reverse_lazy("health_medication_list")

    def form_valid(self, form):
        obj = form.instance
        if obj.student_id:
            obj.class_ref = obj.class_ref or obj.student.current_class
            obj.stream = obj.stream or obj.student.stream
        return super().form_valid(form)


class MedicationRecordDeleteView(LoginRequiredMixin, SchoolScopedMixin, DeleteView):
    model = MedicationRecord
    template_name = "dashboard/health/confirm_delete.html"
    success_url = reverse_lazy("health_medication_list")



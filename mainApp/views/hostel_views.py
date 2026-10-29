from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import JsonResponse
from django.urls import reverse_lazy
from django.views.generic import ListView, CreateView, UpdateView, DeleteView
from django.db.models import Count, Q

from mainApp.models.models import *
from mainApp.models.student import Student, Hostel
from mainApp.models.hostel_models import HostelRoom, HostelBed, HostelAssignment
from mainApp.forms.hostel_forms import (
    HostelForm, HostelRoomForm, HostelBedForm, HostelAssignmentForm
)
from django.views.decorators.http import require_GET

def get_school(request):
    return getattr(getattr(request.user, "profile", None), "school", None)


class SchoolScopedMixin:
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


# -----------------------------
# Boarders (ONLY BOARDING)
# -----------------------------
class BoarderListView(LoginRequiredMixin, ListView):
    template_name = "dashboard/hostel/boarders_list.html"
    model = Student
    context_object_name = "students"
    paginate_by = 2000  # DataTables handles paging

    def get_queryset(self):
        school = get_school(self.request)
        qs = Student.objects.filter(
            school=school,
            is_active=True,
            residence="BOARDING",
        ).select_related("current_class", "stream", "hostel").order_by("name")

        q = (self.request.GET.get("q") or "").strip()
        hostel_id = (self.request.GET.get("hostel_id") or "").strip()
        class_id = (self.request.GET.get("class_id") or "").strip()
        stream_id = (self.request.GET.get("stream_id") or "").strip()

        if q:
            qs = qs.filter(Q(name__icontains=q) | Q(admission_number__icontains=q))
        if hostel_id:
            qs = qs.filter(hostel_id=hostel_id)
        if class_id:
            qs = qs.filter(current_class_id=class_id)
        if stream_id:
            qs = qs.filter(stream_id=stream_id)

        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        school = get_school(self.request)

        class_id = (self.request.GET.get("class_id") or "").strip()

        ctx["title"] = "Boarders"
        ctx["total_boarders"] = Student.objects.filter(
            school=school, is_active=True, residence="BOARDING"
        ).count()

        ctx["hostels"] = Hostel.objects.filter(school=school).order_by("name")
        ctx["classes"] = Class.objects.filter(school=school).order_by("name")

        # preload streams if class selected
        ctx["streams"] = Stream.objects.filter(class_ref_id=class_id).order_by("name") if class_id else Stream.objects.none()

        # keep selected values
        ctx["q"] = (self.request.GET.get("q") or "").strip()
        ctx["selected_hostel_id"] = (self.request.GET.get("hostel_id") or "").strip()
        ctx["selected_class_id"] = (self.request.GET.get("class_id") or "").strip()
        ctx["selected_stream_id"] = (self.request.GET.get("stream_id") or "").strip()

        return ctx

# -----------------------------
# Hostels CRUD
# -----------------------------
class HostelListView(LoginRequiredMixin, SchoolScopedMixin, ListView):
    model = Hostel
    template_name = "dashboard/hostel/hostel_list.html"
    context_object_name = "rows"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        school = get_school(self.request)

        class_id = (self.request.GET.get("class_id") or "").strip()
        stream_id = (self.request.GET.get("stream_id") or "").strip()

        # base assignments (active only)
        qs = HostelAssignment.objects.filter(school=school, is_active=True)

        if class_id:
            qs = qs.filter(student__current_class_id=class_id)
        if stream_id:
            qs = qs.filter(student__stream_id=stream_id)

        occupancy = qs.values("hostel_id").annotate(c=Count("id"))
        occ_map = {x["hostel_id"]: x["c"] for x in occupancy}

        for h in ctx["rows"]:
            h.live_occupancy = occ_map.get(h.id, 0)

        ctx["title"] = "Hostels"
        ctx["classes"] = Class.objects.filter(school=school).order_by("name")
        ctx["selected_class_id"] = class_id
        ctx["selected_stream_id"] = stream_id

        # (optional) preload streams if class selected, else empty
        ctx["streams"] = Stream.objects.filter(class_ref_id=class_id).order_by("name") if class_id else Stream.objects.none()

        return ctx

class HostelCreateView(LoginRequiredMixin, SchoolScopedMixin, CreateView):
    model = Hostel
    form_class = HostelForm
    template_name = "dashboard/hostel/hostel_form.html"
    success_url = reverse_lazy("hostel_list")


class HostelUpdateView(LoginRequiredMixin, SchoolScopedMixin, UpdateView):
    model = Hostel
    form_class = HostelForm
    template_name = "dashboard/hostel/hostel_form.html"
    success_url = reverse_lazy("hostel_list")


class HostelDeleteView(LoginRequiredMixin, SchoolScopedMixin, DeleteView):
    model = Hostel
    template_name = "dashboard/confirm_delete.html"
    success_url = reverse_lazy("hostel_list")


# -----------------------------
# Rooms CRUD
# -----------------------------
class HostelRoomListView(LoginRequiredMixin, SchoolScopedMixin, ListView):
    model = HostelRoom
    template_name = "dashboard/hostel/room_list.html"
    context_object_name = "rows"

    def get_queryset(self):
        return super().get_queryset().select_related("hostel").order_by("hostel__name", "room_number")


class HostelRoomCreateView(LoginRequiredMixin, SchoolScopedMixin, CreateView):
    model = HostelRoom
    form_class = HostelRoomForm
    template_name = "dashboard/hostel/room_form.html"
    success_url = reverse_lazy("hostel_room_list")


class HostelRoomUpdateView(LoginRequiredMixin, SchoolScopedMixin, UpdateView):
    model = HostelRoom
    form_class = HostelRoomForm
    template_name = "dashboard/hostel/room_form.html"
    success_url = reverse_lazy("hostel_room_list")


class HostelRoomDeleteView(LoginRequiredMixin, SchoolScopedMixin, DeleteView):
    model = HostelRoom
    template_name = "dashboard/confirm_delete.html"
    success_url = reverse_lazy("hostel_room_list")


# -----------------------------
# Beds CRUD
# -----------------------------
class HostelBedListView(LoginRequiredMixin, SchoolScopedMixin, ListView):
    model = HostelBed
    template_name = "dashboard/hostel/bed_list.html"
    context_object_name = "rows"

    def get_queryset(self):
        return super().get_queryset().select_related("room", "room__hostel")


class HostelBedCreateView(LoginRequiredMixin, SchoolScopedMixin, CreateView):
    model = HostelBed
    form_class = HostelBedForm
    template_name = "dashboard/hostel/bed_form.html"
    success_url = reverse_lazy("hostel_bed_list")


class HostelBedUpdateView(LoginRequiredMixin, SchoolScopedMixin, UpdateView):
    model = HostelBed
    form_class = HostelBedForm
    template_name = "dashboard/hostel/bed_form.html"
    success_url = reverse_lazy("hostel_bed_list")


class HostelBedDeleteView(LoginRequiredMixin, SchoolScopedMixin, DeleteView):
    model = HostelBed
    template_name = "dashboard/confirm_delete.html"
    success_url = reverse_lazy("hostel_bed_list")


# -----------------------------
# Assignments CRUD
# -----------------------------
class HostelAssignmentListView(LoginRequiredMixin, SchoolScopedMixin, ListView):
    model = HostelAssignment
    template_name = "dashboard/hostel/assignment_list.html"
    context_object_name = "rows"

    def get_queryset(self):
        qs = super().get_queryset().select_related("student", "hostel", "room", "bed")
        active = self.request.GET.get("active")
        if active in ("0", "1"):
            qs = qs.filter(is_active=(active == "1"))
        return qs


class HostelAssignmentCreateView(LoginRequiredMixin, SchoolScopedMixin, CreateView):
    model = HostelAssignment
    template_name = "dashboard/hostel/assignment_form.html"
    success_url = reverse_lazy("hostel_assignment_list")

    def get_form_class(self):
        return HostelAssignmentForm

    def get_form_kwargs(self):
        kw = super().get_form_kwargs()
        kw["school"] = get_school(self.request)
        return kw


class HostelAssignmentUpdateView(LoginRequiredMixin, SchoolScopedMixin, UpdateView):
    model = HostelAssignment
    template_name = "dashboard/hostel/assignment_form.html"
    success_url = reverse_lazy("hostel_assignment_list")

    def get_form_class(self):
        return HostelAssignmentForm

    def get_form_kwargs(self):
        kw = super().get_form_kwargs()
        kw["school"] = get_school(self.request)
        return kw


class HostelAssignmentDeleteView(LoginRequiredMixin, SchoolScopedMixin, DeleteView):
    model = HostelAssignment
    template_name = "dashboard/confirm_delete.html"
    success_url = reverse_lazy("hostel_assignment_list")




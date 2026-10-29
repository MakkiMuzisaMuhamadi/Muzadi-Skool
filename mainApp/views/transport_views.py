# mainApp/views/transport_views.py
from datetime import timezone

from django.contrib.auth.decorators import login_required
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages

from mainApp.decorators import role_required
from mainApp.models.transport_models import TransportVehicle, TransportRoute, TransportAssignment
from mainApp.forms.transport_forms import TransportVehicleForm, TransportRouteForm, TransportAssignmentForm

def _school(request):
    return getattr(getattr(request.user, "profile", None), "school", None)

@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
def vehicle_list(request):
    school = _school(request)
    qs = TransportVehicle.objects.filter(school=school)
    return render(request, "dashboard/transport/vehicle_list.html", {"rows": qs, "title": "Vehicles"})

@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
def vehicle_create(request):
    school = _school(request)
    form = TransportVehicleForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        obj = form.save(commit=False)
        obj.school = school
        obj.save()
        messages.success(request, "Vehicle saved.")
        return redirect("transport_vehicle_list")
    return render(request, "dashboard/transport/vehicle_form.html", {"form": form, "title": "Add Vehicle"})

@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
def vehicle_update(request, pk):
    school = _school(request)
    obj = get_object_or_404(TransportVehicle, pk=pk, school=school)
    form = TransportVehicleForm(request.POST or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Vehicle updated.")
        return redirect("transport_vehicle_list")
    return render(request, "dashboard/transport/vehicle_form.html", {"form": form, "title": "Edit Vehicle"})

@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
def route_list(request):
    school = _school(request)
    qs = TransportRoute.objects.filter(school=school)
    return render(request, "dashboard/transport/route_list.html", {"rows": qs, "title": "Routes"})

@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
def route_create(request):
    school = _school(request)
    form = TransportRouteForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        obj = form.save(commit=False)
        obj.school = school
        obj.save()
        messages.success(request, "Route saved.")
        return redirect("transport_route_list")
    return render(request, "dashboard/transport/route_form.html", {"form": form, "title": "Add Route"})

@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
def route_update(request, pk):
    school = _school(request)
    obj = get_object_or_404(TransportRoute, pk=pk, school=school)
    form = TransportRouteForm(request.POST or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Route updated.")
        return redirect("transport_route_list")
    return render(request, "dashboard/transport/route_form.html", {"form": form, "title": "Edit Route"})

@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
def transport_students(request):
    """
    One screen: assign/unassign students to routes.
    """
    school = _school(request)

    assignments = TransportAssignment.objects.filter(school=school, is_active=True).select_related("student","route")
    form = TransportAssignmentForm(request.POST or None)

    # Restrict dropdowns to school
    form.fields["route"].queryset = TransportRoute.objects.filter(school=school, is_active=True)
    form.fields["student"].queryset = form.fields["student"].queryset.filter(school=school, is_active=True)

    if request.method == "POST" and form.is_valid():
        obj = form.save(commit=False)
        obj.school = school
        obj.save()
        messages.success(request, "Student assigned to transport.")
        return redirect("transport_students")

    return render(request, "dashboard/transport/transport_students.html", {
        "title": "Transport Students",
        "form": form,
        "assignments": assignments,
    })

@login_required
@role_required(["SUPER_ADMIN", "ADMIN"])
def transport_unassign(request, pk):
    school = _school(request)
    obj = get_object_or_404(TransportAssignment, pk=pk, school=school, is_active=True)
    obj.is_active = False
    obj.end_date = obj.end_date or timezone.now().date()
    obj.save()
    messages.success(request, "Student removed from transport.")
    return redirect("transport_students")
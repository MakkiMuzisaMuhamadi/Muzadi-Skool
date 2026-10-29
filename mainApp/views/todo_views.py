from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from mainApp.forms.todo_forms import TodoTaskForm
from mainApp.models.todo_models import TodoTask


def get_user_school(request):
    return getattr(request.user, "school", None) or getattr(getattr(request.user, "profile", None), "school", None)


@login_required
def todo_list(request):
    school = get_user_school(request)
    qs = TodoTask.objects.filter(user=request.user, school=school)

    status = request.GET.get("status")
    priority = request.GET.get("priority")
    q = request.GET.get("q")

    if status:
        qs = qs.filter(status=status)
    if priority:
        qs = qs.filter(priority=priority)
    if q:
        qs = qs.filter(title__icontains=q)

    context = {
        "items": qs,
        "pending_count": TodoTask.objects.filter(user=request.user, school=school, status="PENDING").count(),
        "progress_count": TodoTask.objects.filter(user=request.user, school=school, status="IN_PROGRESS").count(),
        "completed_count": TodoTask.objects.filter(user=request.user, school=school, status="COMPLETED").count(),
    }
    return render(request, "dashboard/todo/todo_list.html", context)


@login_required
def todo_create(request):
    school = get_user_school(request)
    form = TodoTaskForm(request.POST or None)

    if request.method == "POST":
        if form.is_valid():
            obj = form.save(commit=False)
            obj.user = request.user
            obj.school = school
            if obj.status == "COMPLETED" and not obj.completed_at:
                obj.completed_at = timezone.now()
            obj.save()
            messages.success(request, "Task created successfully.")
            return redirect("todo_list")

    return render(request, "dashboard/todo/todo_form.html", {"form": form, "title": "Create Task"})


@login_required
def todo_update(request, pk):
    school = get_user_school(request)
    obj = get_object_or_404(TodoTask, pk=pk, user=request.user, school=school)
    form = TodoTaskForm(request.POST or None, instance=obj)

    if request.method == "POST":
        if form.is_valid():
            task = form.save(commit=False)
            if task.status == "COMPLETED" and not task.completed_at:
                task.completed_at = timezone.now()
            elif task.status != "COMPLETED":
                task.completed_at = None
            task.save()
            messages.success(request, "Task updated successfully.")
            return redirect("todo_list")

    return render(request, "dashboard/todo/todo_form.html", {"form": form, "title": "Update Task"})


@login_required
def todo_toggle_complete(request, pk):
    school = get_user_school(request)
    obj = get_object_or_404(TodoTask, pk=pk, user=request.user, school=school)

    if obj.status == "COMPLETED":
        obj.status = "PENDING"
        obj.completed_at = None
    else:
        obj.status = "COMPLETED"
        obj.completed_at = timezone.now()

    obj.save()
    messages.success(request, "Task status updated.")
    return redirect("todo_list")


@login_required
def todo_delete(request, pk):
    school = get_user_school(request)
    obj = get_object_or_404(TodoTask, pk=pk, user=request.user, school=school)

    if request.method == "POST":
        obj.delete()
        messages.success(request, "Task deleted successfully.")
        return redirect("todo_list")

    return render(request, "dashboard/todo/todo_delete.html", {"item": obj})
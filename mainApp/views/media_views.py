"""
Private files: /media/... is no longer a public folder.

Every file request comes here first. Django checks that the signed-in user
belongs to the school that owns the file, then sends it:
  * MEDIA_SERVE_MODE = "django" (laptop): Django sends the file itself
  * MEDIA_SERVE_MODE = "nginx"  (server): Django answers with X-Accel-Redirect
    and nginx sends the file from an "internal" location (fast, no Python).

Report cards are never sent to students / parents from here: they must use
their portal pages, which apply the "fees owed" hold.
"""
import mimetypes
import os
import re

from django.apps import apps
from django.conf import settings
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import redirect

from mainApp import tenancy

# Anyone may see these (they appear on the login page and on printed documents).
PUBLIC_PREFIXES = ("schoolLogos/",)

# prefix -> (model label, file field, path from the model to its School)
OWNERS = [
    ("report_cards/", "mainApp.ReportCardSnapshot", "pdf", "run__school"),
    ("teachers_profile_drafts/", "mainApp.TeacherDraft", "profile_photo", "created_by__profile__school"),
    ("teachers_profile/", "mainApp.Teacher", "profile_photo", "user__profile__school"),
    ("staff_profile/", "mainApp.NonTeachingStaff", "profile_photo", "user__profile__school"),
    ("parents_profile/", "mainApp.Parent", "profile_photo", "school"),
    ("candidates_profile/", "mainApp.Student", "profile_photo", "school"),
    ("student_bulk_uploads/", "mainApp.StudentBulkUploadBatch", "file", "school"),
    ("report_signatures/", "mainApp.ReportCardSignatureRole", "signature_image", "template__school"),
]

PORTAL_ROLES = ("STUDENT", "PARENT")


def _owner_school_id(rel_path):
    for prefix, label, field, school_path in OWNERS:
        if rel_path.startswith(prefix):
            model = apps.get_model(label)
            with tenancy.guard_disabled():
                row = (model._base_manager.filter(**{field: rel_path})
                       .values_list(f"{school_path}_id", flat=True).first())
            return prefix, row
    return None, None


def _portal_may_see(user, prefix, rel_path):
    """Students / parents: only their own (or their children's) photos."""
    if prefix == "candidates_profile/":
        from mainApp.models.student import Student
        own = Student._base_manager.filter(profile_photo=rel_path)
        return own.filter(user=user).exists() or own.filter(guardian_links__parent__user=user).exists()
    if prefix == "parents_profile/":
        from mainApp.models.student import Parent
        return Parent._base_manager.filter(profile_photo=rel_path, user=user).exists()
    return False


def _send(rel_path, full_path):
    if getattr(settings, "MEDIA_SERVE_MODE", "django") == "nginx":
        resp = HttpResponse()
        resp["X-Accel-Redirect"] = settings.MEDIA_PROTECTED_PREFIX + rel_path
        resp["Content-Type"] = mimetypes.guess_type(full_path)[0] or "application/octet-stream"
        return resp
    return FileResponse(open(full_path, "rb"))


def protected_media(request, path):
    rel_path = path.replace("\\", "/").lstrip("/")
    if ".." in rel_path.split("/") or re.search(r"[\x00]", rel_path):
        raise Http404
    root = os.path.realpath(settings.MEDIA_ROOT)
    full_path = os.path.realpath(os.path.join(root, rel_path))
    if not full_path.startswith(root + os.sep) or not os.path.isfile(full_path):
        raise Http404

    if rel_path.startswith(PUBLIC_PREFIXES):
        return _send(rel_path, full_path)

    user = request.user
    if not user.is_authenticated:
        from django.urls import reverse
        return redirect(f"{reverse('login')}?next={request.path}")
    if tenancy.is_platform_user(user):
        return _send(rel_path, full_path)

    prefix, owner_school_id = _owner_school_id(rel_path)
    my_school_id = getattr(getattr(request, "school", None), "id", None) or tenancy.user_school_id(user)
    if prefix is None:
        # A file no model claims (old exports etc.): school staff only, never students / parents.
        if tenancy.user_role(user) in PORTAL_ROLES:
            raise Http404
        return _send(rel_path, full_path)

    if owner_school_id is None or owner_school_id != my_school_id:
        raise Http404
    if tenancy.user_role(user) in PORTAL_ROLES and not _portal_may_see(user, prefix, rel_path):
        raise Http404
    return _send(rel_path, full_path)

from django.contrib.auth.mixins import LoginRequiredMixin
from django.urls import reverse_lazy
from django.views.generic import ListView, CreateView, UpdateView, DeleteView

from mainApp.forms.report_terms_forms import (
    ReportAutoCommentRuleForm,
    ReportKeywordScoreBandForm,
    ReportKeywordDefinitionForm,
    ReportGradeDescriptorForm,
    ReportSubjectCommentRuleForm,
)
from mainApp.models.ple_models import ReportSubjectCommentRule
from mainApp.models.report_terms_models import *
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse

from mainApp.models.uce_grading_models import GradingScale, Grade

def get_school(request):
    return getattr(getattr(request.user, "profile", None), "school", None)


REPORT_SETUP_ROLES = ("SUPER_ADMIN", "ADMIN")


def _is_setup_admin(user):
    if user.is_superuser:
        return True
    return getattr(getattr(user, "profile", None), "role", "") in REPORT_SETUP_ROLES


class SchoolScopedMixin:
    def dispatch(self, request, *args, **kwargs):
        # Report setup (comment rules, descriptors, bands) is for admins only.
        if request.user.is_authenticated and not _is_setup_admin(request.user):
            from django.core.exceptions import PermissionDenied
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        qs = super().get_queryset()
        school = get_school(self.request)
        return qs.filter(school=school) if school else qs.none()

    def form_valid(self, form):
        """
        Safe for Create/Update AND Delete:
        - DeleteView uses a plain Form => no `instance`
        """
        if hasattr(form, "instance"):
            school = get_school(self.request)
            if school and hasattr(form.instance, "school_id") and not form.instance.school_id:
                form.instance.school = school
        return super().form_valid(form)


class RequestFormKwargsMixin:
    """
    Adds `request` into form kwargs ONLY for views that actually use ModelForms
    (CreateView / UpdateView). DeleteView must NOT use this.
    """
    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["request"] = self.request
        return kwargs

class AutoCommentRuleList(LoginRequiredMixin, SchoolScopedMixin, ListView):
    model = ReportAutoCommentRule
    template_name = "dashboard/report_terms/auto_comment_list.html"
    context_object_name = "rows"

    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .select_related("level", "school")
            .order_by("level__order", "level__name", "comment_for", "order", "id")
        )

class AutoCommentRuleCreate(LoginRequiredMixin, SchoolScopedMixin, RequestFormKwargsMixin, CreateView):
    model = ReportAutoCommentRule
    form_class = ReportAutoCommentRuleForm
    template_name = "dashboard/report_terms/auto_comment_form.html"
    success_url = reverse_lazy("report_auto_comment_list")



class AutoCommentRuleUpdate(LoginRequiredMixin, SchoolScopedMixin, RequestFormKwargsMixin, UpdateView):
    model = ReportAutoCommentRule
    form_class = ReportAutoCommentRuleForm
    template_name = "dashboard/report_terms/auto_comment_form.html"
    success_url = reverse_lazy("report_auto_comment_list")


class AutoCommentRuleDelete(LoginRequiredMixin, SchoolScopedMixin, DeleteView):
    model = ReportAutoCommentRule
    template_name = "dashboard/report_terms/confirm_delete.html"
    success_url = reverse_lazy("report_auto_comment_list")



# ---------- Keyword Bands ----------
class KeywordBandList(LoginRequiredMixin, SchoolScopedMixin, ListView):
    model = ReportKeywordScoreBand
    template_name = "dashboard/report_terms/keyword_band_list.html"
    context_object_name = "rows"


class KeywordBandCreate(LoginRequiredMixin, SchoolScopedMixin, RequestFormKwargsMixin, CreateView):
    model = ReportKeywordScoreBand
    form_class = ReportKeywordScoreBandForm
    template_name = "dashboard/report_terms/keyword_band_form.html"
    success_url = reverse_lazy("report_keyword_band_list")



class KeywordBandUpdate(LoginRequiredMixin, SchoolScopedMixin, RequestFormKwargsMixin, UpdateView):
    model = ReportKeywordScoreBand
    form_class = ReportKeywordScoreBandForm
    template_name = "dashboard/report_terms/keyword_band_form.html"
    success_url = reverse_lazy("report_keyword_band_list")



class KeywordBandDelete(LoginRequiredMixin, SchoolScopedMixin, DeleteView):
    model = ReportKeywordScoreBand
    template_name = "dashboard/report_terms/confirm_delete.html"
    success_url = reverse_lazy("report_keyword_band_list")



# ---------- Keyword Definitions ----------
class KeywordDefList(LoginRequiredMixin, SchoolScopedMixin, ListView):
    model = ReportKeywordDefinition
    template_name = "dashboard/report_terms/keyword_def_list.html"
    context_object_name = "rows"


class KeywordDefCreate(LoginRequiredMixin, SchoolScopedMixin, RequestFormKwargsMixin, CreateView):
    model = ReportKeywordDefinition
    form_class = ReportKeywordDefinitionForm
    template_name = "dashboard/report_terms/keyword_def_form.html"
    success_url = reverse_lazy("report_keyword_def_list")



class KeywordDefUpdate(LoginRequiredMixin, SchoolScopedMixin, RequestFormKwargsMixin, UpdateView):
    model = ReportKeywordDefinition
    form_class = ReportKeywordDefinitionForm
    template_name = "dashboard/report_terms/keyword_def_form.html"
    success_url = reverse_lazy("report_keyword_def_list")



class KeywordDefDelete(LoginRequiredMixin, SchoolScopedMixin, DeleteView):
    model = ReportKeywordDefinition
    template_name = "dashboard/report_terms/confirm_delete.html"
    success_url = reverse_lazy("report_keyword_def_list")


# ---------- Grade Descriptors ----------
class GradeDescList(LoginRequiredMixin, SchoolScopedMixin, ListView):
    model = ReportGradeDescriptor
    template_name = "dashboard/report_terms/grade_desc_list.html"
    context_object_name = "rows"


class GradeDescCreate(LoginRequiredMixin, SchoolScopedMixin, RequestFormKwargsMixin, CreateView):
    model = ReportGradeDescriptor
    form_class = ReportGradeDescriptorForm
    template_name = "dashboard/report_terms/grade_desc_form.html"
    success_url = reverse_lazy("report_grade_desc_list")



class GradeDescUpdate(LoginRequiredMixin, SchoolScopedMixin, RequestFormKwargsMixin, UpdateView):
    model = ReportGradeDescriptor
    form_class = ReportGradeDescriptorForm
    template_name = "dashboard/report_terms/grade_desc_form.html"
    success_url = reverse_lazy("report_grade_desc_list")


class GradeDescDelete(LoginRequiredMixin, SchoolScopedMixin, DeleteView):
    model = ReportGradeDescriptor
    template_name = "dashboard/report_terms/confirm_delete.html"
    success_url = reverse_lazy("report_grade_desc_list")

# ---------- Subject Comments ----------
class SubjectCommentRuleList(LoginRequiredMixin, SchoolScopedMixin, ListView):
    model = ReportSubjectCommentRule
    template_name = "dashboard/report_terms/subject_comment_list.html"
    context_object_name = "rows"


class SubjectCommentRuleCreate(LoginRequiredMixin, SchoolScopedMixin, RequestFormKwargsMixin, CreateView):
    model = ReportSubjectCommentRule
    form_class = ReportSubjectCommentRuleForm
    template_name = "dashboard/report_terms/subject_comment_form.html"
    success_url = reverse_lazy("report_subject_comment_list")

    def form_valid(self, form):
        try:
            return super().form_valid(form)
        except ValidationError as e:
            form.add_error(None, e)
            return self.form_invalid(form)


class SubjectCommentRuleUpdate(LoginRequiredMixin, SchoolScopedMixin, RequestFormKwargsMixin, UpdateView):
    model = ReportSubjectCommentRule
    form_class = ReportSubjectCommentRuleForm
    template_name = "dashboard/report_terms/subject_comment_form.html"
    success_url = reverse_lazy("report_subject_comment_list")

    def form_valid(self, form):
        try:
            return super().form_valid(form)
        except ValidationError as e:
            form.add_error(None, e)
            return self.form_invalid(form)

class SubjectCommentRuleDelete(LoginRequiredMixin, SchoolScopedMixin, DeleteView):
    model = ReportSubjectCommentRule
    template_name = "dashboard/report_terms/confirm_delete.html"
    success_url = reverse_lazy("report_subject_comment_list")


# ------------------------------------------------------------------
# One click: load the standard A-E descriptors, 1-3 identifiers and key words
# ------------------------------------------------------------------
from django.contrib import messages
from django.shortcuts import redirect
from django.views.decorators.http import require_POST


@require_POST
@login_required
def report_load_standard_terms(request):
    from django.core.exceptions import PermissionDenied
    from mainApp.models.models import EducationLevel
    from mainApp.report_configs.report_defaults import load_standard_report_terms

    if not _is_setup_admin(request.user):
        raise PermissionDenied
    school = get_school(request)
    code = (request.POST.get("level") or "UCE").upper()
    level = EducationLevel.objects.filter(code=code).first()
    nxt = request.POST.get("next") or ""
    back = nxt if nxt.startswith("/") and not nxt.startswith("//") else reverse_lazy("report_grade_desc_list")

    if not school or not level:
        messages.error(request, f"Could not find the school or the {code} level.")
        return redirect(back)

    added = load_standard_report_terms(school=school, level=level, user=request.user)
    total = sum(added.values())
    if total:
        messages.success(
            request,
            f"Added {added['grade_descriptors']} grade descriptor(s), {added['score_bands']} score identifier(s) "
            f"and {added['key_words']} key word(s) for {code}. Existing entries were left as they were - "
            f"please check the ranges match your school's policy.",
        )
    else:
        messages.info(request, f"The standard {code} entries are already there. Nothing was changed.")
    return redirect(back)


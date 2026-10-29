# mainApp/forms/report_terms_forms.py
from django import forms
from django.db.models import Q
from django.db.models import Max
from mainApp.models.models import EducationLevel
from mainApp.models.ple_models import PLEDivision, PLEGrade, PLEGradingScale, ReportSubjectCommentRule
from mainApp.models.report_terms_models import (
    ReportAutoCommentRule,
    ReportKeywordScoreBand,
    ReportKeywordDefinition,
    ReportGradeDescriptor,
)

from mainApp.models.uce_grading_models import GradingScale, Grade  # your existing models
from django.core.exceptions import ValidationError

BOOTSTRAP_INPUT = "form-control"
BOOTSTRAP_SELECT = "form-control"  
BOOTSTRAP_CHECK = "form-check-input"
BOOTSTRAP_TEXTAREA = "form-control"


UACE_POINT_RULES = [
    ("UACE_POINTS_00_05", "0 - 5 Points", 0, 5),
    ("UACE_POINTS_06_10", "6 - 10 Points", 6, 10),
    ("UACE_POINTS_11_15", "11 - 15 Points", 11, 15),
    ("UACE_POINTS_16_18", "16 - 18 Points", 16, 18),
    ("UACE_POINTS_19_20", "19 - 20 Points", 19, 20),
]

def get_school(request):
    return getattr(getattr(request.user, "profile", None), "school", None)

def active_scale_for_school_level(school, level_id):
    if not school or not level_id:
        return None
    return (
        GradingScale.objects
        .filter(school=school, level_id=level_id, is_active=True)
        .order_by("-id")
        .first()
    )
class SchoolLevelFormMixin(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        self.request = kwargs.pop("request", None)
        super().__init__(*args, **kwargs)

        if "level" in self.fields and self.request:
            self.fields["level"].queryset = school_levels_queryset(self.request)

        for name, field in self.fields.items():
            widget = field.widget
            if isinstance(widget, forms.CheckboxInput):
                widget.attrs.setdefault("class", BOOTSTRAP_CHECK)
                continue
            if isinstance(widget, (forms.Select, forms.SelectMultiple)):
                widget.attrs.setdefault("class", BOOTSTRAP_SELECT)
            else:
                widget.attrs.setdefault("class", BOOTSTRAP_INPUT)
            widget.attrs.setdefault("autocomplete", "off")

def school_levels_queryset(request):
    school = getattr(getattr(request.user, "profile", None), "school", None)
    if not school:
        return EducationLevel.objects.none()
    return school.level.filter(is_active=True).order_by("order", "name")

class ReportAutoCommentRuleForm(SchoolLevelFormMixin):
    grade_letter = forms.ChoiceField(
        required=True,
        choices=[("", "— Select Rule —")],
        widget=forms.Select(attrs={
            "class": BOOTSTRAP_SELECT,
            "data-grade-select": "1",
        }),
        label="Result Rule",
    )

    class Meta:
        model = ReportAutoCommentRule
        fields = [
            "level",
            "comment_for",
            "grade_letter",
            "min_percentage",
            "max_percentage",
            "comment_text",
            "is_active",
        ]
        widgets = {
            "level": forms.Select(attrs={
                "class": BOOTSTRAP_SELECT,
                "data-level-select": "1",
            }),
            "comment_for": forms.Select(attrs={
                "class": BOOTSTRAP_SELECT,
            }),
            "min_percentage": forms.NumberInput(attrs={
                "class": BOOTSTRAP_INPUT,
                "readonly": "readonly",
            }),
            "max_percentage": forms.NumberInput(attrs={
                "class": BOOTSTRAP_INPUT,
                "readonly": "readonly",
            }),
            "comment_text": forms.Textarea(attrs={
                "class": BOOTSTRAP_TEXTAREA,
                "rows": 4,
                "placeholder": "Example: Excellent performance. Keep it up.",
            }),
            "is_active": forms.CheckboxInput(attrs={
                "class": BOOTSTRAP_CHECK,
            }),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self._rule_map = {}

        school = getattr(
            getattr(getattr(self.request, "user", None), "profile", None),
            "school",
            None
        ) if self.request else None

        self.school = school

        if school and not self.instance.school_id:
            self.instance.school = school

        level_id = None

        if self.data:
            level_id = self.data.get("level")
        elif getattr(self.instance, "level_id", None):
            level_id = self.instance.level_id
        elif self.initial.get("level"):
            level_id = self.initial.get("level")

        level = None
        if level_id:
            level = EducationLevel.objects.filter(id=level_id).first()

        selected_rule = ""
        if self.data:
            selected_rule = (self.data.get("grade_letter") or "").strip()
        else:
            selected_rule = (getattr(self.instance, "grade_letter", "") or "").strip()

        self.fields["grade_letter"].choices = self.get_result_rule_choices(
            school=school,
            level=level,
            selected_rule=selected_rule,
        )

        if selected_rule:
            self.initial["grade_letter"] = selected_rule

        if not self.data and self.instance and self.instance.pk:
            self.initial["min_percentage"] = self.instance.min_percentage
            self.initial["max_percentage"] = self.instance.max_percentage

        self.fields["min_percentage"].required = False
        self.fields["max_percentage"].required = False

    def get_result_rule_choices(self, school, level, selected_rule=None):
        choices = [("", "— Select Rule —")]
        self._rule_map = {}

        if not school or not level:
            if selected_rule:
                choices.append((selected_rule, selected_rule))
            return choices

        level_code = (getattr(level, "code", "") or "").upper().strip()

        # PLE Class Teacher / Head Teacher comments must use divisions, not subject grades.
        if level_code == "PLE":
            scale = (
                PLEGradingScale.objects
                .filter(
                    school=school,
                    level=level,
                    is_active=True,
                )
                .order_by("-id")
                .first()
            )

            if scale:
                divisions = (
                    PLEDivision.objects
                    .filter(grading_scale=scale)
                    .order_by("order", "min_aggregate", "division")
                )

                for d in divisions:
                    value = (d.division or "").strip()
                    label = f"{value} ({d.min_aggregate}-{d.max_aggregate} aggregates)"

                    choices.append((value, label))
                    self._rule_map[value.upper()] = {
                        "min": d.min_aggregate,
                        "max": d.max_aggregate,
                        "label": label,
                        "type": "DIVISION",
                    }

        elif level_code == "UACE":
            for value, label, min_points, max_points in UACE_POINT_RULES:
                choices.append((value, label))
                self._rule_map[value.upper()] = {
                    "min": min_points,
                    "max": max_points,
                    "label": label,
                    "type": "UACE_POINTS",
                }

        # UCE keeps using grade rules as before.
        elif level_code == "UCE":
            scale = (
                GradingScale.objects
                .filter(
                    school=school,
                    level=level,
                    is_active=True,
                )
                .order_by("-id")
                .first()
            )

            if scale:
                grades = (
                    Grade.objects
                    .filter(grading_scale=scale)
                    .order_by("order", "-min_percentage", "grade_letter")
                )

                for g in grades:
                    value = (g.grade_letter or "").strip().upper()
                    label = f"{value} ({g.min_percentage}-{g.max_percentage})"

                    if getattr(g, "grade_name", ""):
                        label = f"{label} - {g.grade_name}"

                    choices.append((value, label))
                    self._rule_map[value.upper()] = {
                        "min": int(float(g.min_percentage)),
                        "max": int(float(g.max_percentage)),
                        "label": label,
                        "type": "GRADE",
                    }

        if selected_rule:
            selected_rule = selected_rule.strip()
            existing_values = [choice[0] for choice in choices]

            if selected_rule not in existing_values:
                choices.append((selected_rule, selected_rule))

        return choices

    def clean_grade_letter(self):
        value = self.cleaned_data.get("grade_letter") or ""
        return value.strip()

    def clean(self):
        cleaned = super().clean()

        level = cleaned.get("level")
        comment_for = cleaned.get("comment_for")
        selected_rule = (cleaned.get("grade_letter") or "").strip()

        if not selected_rule:
            self.add_error("grade_letter", "Please select a result rule.")
            return cleaned

        lookup_key = selected_rule.upper()

        if lookup_key not in self._rule_map:
            self.add_error(
                "grade_letter",
                f"Selected rule '{selected_rule}' was not found in the active grading rules for this level."
            )
            return cleaned

        cleaned["min_percentage"] = self._rule_map[lookup_key]["min"]
        cleaned["max_percentage"] = self._rule_map[lookup_key]["max"]

        if self.school and level and comment_for and selected_rule:
            exists = (
                ReportAutoCommentRule.objects
                .filter(
                    school=self.school,
                    level=level,
                    comment_for=comment_for,
                    grade_letter__iexact=selected_rule,
                )
                .exclude(pk=self.instance.pk)
                .exists()
            )

            if exists:
                self.add_error(
                    "grade_letter",
                    f"A {dict(ReportAutoCommentRule.COMMENT_FOR_CHOICES).get(comment_for, comment_for)} comment for '{selected_rule}' already exists for this level. Please edit the existing one instead."
                )

        return cleaned

    def save(self, commit=True):
        obj = super().save(commit=False)

        if self.school and not obj.school_id:
            obj.school = self.school

        selected_rule = (self.cleaned_data.get("grade_letter") or "").strip()
        obj.grade_letter = selected_rule

        lookup_key = selected_rule.upper()

        if lookup_key in self._rule_map:
            obj.min_percentage = self._rule_map[lookup_key]["min"]
            obj.max_percentage = self._rule_map[lookup_key]["max"]

        if not obj.pk:
            last = (
                ReportAutoCommentRule.objects
                .filter(
                    school=obj.school,
                    level=obj.level,
                    comment_for=obj.comment_for,
                )
                .aggregate(m=Max("order"))
                .get("m") or 0
            )
            obj.order = last + 1

        if commit:
            obj.save()

        return obj

# -----------------------
# Keyword Score Bands
# -----------------------
class ReportKeywordScoreBandForm(SchoolLevelFormMixin):
    class Meta:
        model = ReportKeywordScoreBand
        fields = ["level", "identifier", "min_score", "max_score", "descriptor", "is_active"]
        widgets = {
            "level": forms.Select(attrs={"class": BOOTSTRAP_SELECT}),

            "identifier": forms.NumberInput(attrs={"class": BOOTSTRAP_INPUT, "min": 1, "max": 10}),
            "min_score": forms.NumberInput(attrs={"class": BOOTSTRAP_INPUT, "step": "0.1"}),
            "max_score": forms.NumberInput(attrs={"class": BOOTSTRAP_INPUT, "step": "0.1"}),

            "descriptor": forms.Textarea(attrs={"class": BOOTSTRAP_TEXTAREA, "rows": 4, "placeholder": "Write descriptor..." }),
            "is_active": forms.CheckboxInput(attrs={"class": BOOTSTRAP_CHECK}),
        }

    def clean(self):
        cleaned = super().clean()
        min_s = cleaned.get("min_score")
        max_s = cleaned.get("max_score")
        if min_s is not None and max_s is not None and min_s > max_s:
            raise forms.ValidationError("Min score cannot be greater than Max score.")
        return cleaned


# -----------------------
# Keyword Definitions
# -----------------------
class ReportKeywordDefinitionForm(SchoolLevelFormMixin):
    class Meta:
        model = ReportKeywordDefinition
        fields = ["level", "key_word", "description", "is_active"]
        widgets = {
            "level": forms.Select(attrs={"class": BOOTSTRAP_SELECT}),
            "key_word": forms.TextInput(attrs={"class": BOOTSTRAP_INPUT, "placeholder": "e.g. Competency, Descriptor, Score"}),
            "description": forms.Textarea(attrs={"class": BOOTSTRAP_TEXTAREA, "rows": 4, "placeholder": "Write description..." }),
            "is_active": forms.CheckboxInput(attrs={"class": BOOTSTRAP_CHECK}),
        }


# -----------------------
# Grade Descriptors
# -----------------------
class ReportGradeDescriptorForm(SchoolLevelFormMixin):
    class Meta:
        model = ReportGradeDescriptor
        fields = ["level", "grade_letter", "min_percentage", "max_percentage", "descriptor", "is_active"]
        widgets = {
            "level": forms.Select(attrs={"class": BOOTSTRAP_SELECT}),

            "grade_letter": forms.TextInput(attrs={"class": BOOTSTRAP_INPUT, "placeholder": "e.g. A, B, C"}),

            "min_percentage": forms.NumberInput(attrs={"class": BOOTSTRAP_INPUT, "min": 0, "max": 100}),
            "max_percentage": forms.NumberInput(attrs={"class": BOOTSTRAP_INPUT, "min": 0, "max": 100}),

            "descriptor": forms.Textarea(attrs={"class": BOOTSTRAP_TEXTAREA, "rows": 5, "placeholder": "Write long grade descriptor..." }),
            "is_active": forms.CheckboxInput(attrs={"class": BOOTSTRAP_CHECK}),
        }

    def clean(self):
        cleaned = super().clean()
        minp = cleaned.get("min_percentage")
        maxp = cleaned.get("max_percentage")
        if minp is not None and maxp is not None and minp > maxp:
            raise forms.ValidationError("Min percentage cannot be greater than Max percentage.")
        return cleaned
    

class ReportSubjectCommentRuleForm(forms.ModelForm):
    grade_letter = forms.ChoiceField(
        required=True,
        choices=[("", "— Select Grade —")],
        widget=forms.Select(attrs={
            "class": BOOTSTRAP_SELECT,
            "data-grade-select": "1",
        })
    )

    class Meta:
        model = ReportSubjectCommentRule
        fields = [
            "level",
            "grade_letter",
            "min_percentage",
            "max_percentage",
            "comment_text",
            "is_active",
        ]
        widgets = {
            "level": forms.Select(attrs={
                "class": BOOTSTRAP_SELECT,
                "data-level-select": "1",
            }),
            "min_percentage": forms.NumberInput(attrs={
                "class": BOOTSTRAP_INPUT,
                "step": "0.01",
                "readonly": "readonly",
            }),
            "max_percentage": forms.NumberInput(attrs={
                "class": BOOTSTRAP_INPUT,
                "step": "0.01",
                "readonly": "readonly",
            }),
            "comment_text": forms.Textarea(attrs={
                "class": BOOTSTRAP_TEXTAREA,
                "rows": 4,
                "placeholder": "Example: Excellent performance. Keep it up.",
            }),
            "is_active": forms.CheckboxInput(attrs={
                "class": "form-check-input",
            }),
        }

    def __init__(self, *args, **kwargs):
        self.request = kwargs.pop("request", None)
        super().__init__(*args, **kwargs)

        self._grade_map = {}

        school = getattr(
            getattr(getattr(self.request, "user", None), "profile", None),
            "school",
            None
        ) if self.request else None

        self.school = school

        if school:
            try:
                self.fields["level"].queryset = school.level.all().order_by("name")
            except Exception:
                self.fields["level"].queryset = EducationLevel.objects.none()
        else:
            self.fields["level"].queryset = EducationLevel.objects.none()

        # Set school early so model validation also knows the school.
        if school and not self.instance.school_id:
            self.instance.school = school

        level_id = None

        if self.data:
            level_id = self.data.get("level")
        elif getattr(self.instance, "level_id", None):
            level_id = self.instance.level_id
        elif self.initial.get("level"):
            level_id = self.initial.get("level")

        level = None
        if level_id:
            level = EducationLevel.objects.filter(id=level_id).first()

        selected_grade = ""
        if self.data:
            selected_grade = (self.data.get("grade_letter") or "").strip().upper()
        else:
            selected_grade = (getattr(self.instance, "grade_letter", "") or "").strip().upper()

        self.fields["grade_letter"].choices = self.get_grade_choices(
            school=school,
            level=level,
            selected_grade=selected_grade,
        )

        if selected_grade:
            self.initial["grade_letter"] = selected_grade

        if not self.data and self.instance and self.instance.pk:
            self.initial["min_percentage"] = self.instance.min_percentage
            self.initial["max_percentage"] = self.instance.max_percentage

        self.fields["min_percentage"].required = False
        self.fields["max_percentage"].required = False

    def get_grade_choices(self, school, level, selected_grade=None):
        choices = [("", "— Select Grade —")]
        self._grade_map = {}

        if not school or not level:
            if selected_grade:
                choices.append((selected_grade, selected_grade))
            return choices

        level_code = (getattr(level, "code", "") or "").upper().strip()

        if level_code == "PLE":
            scale = (
                PLEGradingScale.objects
                .filter(
                    school=school,
                    level=level,
                    is_active=True,
                )
                .order_by("-id")
                .first()
            )

            if scale:
                grades = (
                    PLEGrade.objects
                    .filter(grading_scale=scale)
                    .order_by("order", "aggregate_point", "grade_letter")
                )

                for g in grades:
                    letter = (g.grade_letter or "").strip().upper()
                    label = f"{letter} - {g.grade_name} ({g.min_percentage}-{g.max_percentage})"

                    choices.append((letter, label))
                    self._grade_map[letter] = {
                        "min": g.min_percentage,
                        "max": g.max_percentage,
                        "grade_name": g.grade_name,
                        "aggregate_point": g.aggregate_point,
                    }

        elif level_code == "UCE":
            scale = (
                GradingScale.objects
                .filter(
                    school=school,
                    level=level,
                    is_active=True,
                )
                .order_by("-id")
                .first()
            )

            if scale:
                grades = (
                    Grade.objects
                    .filter(grading_scale=scale)
                    .order_by("order", "-min_percentage", "grade_letter")
                )

                for g in grades:
                    letter = (g.grade_letter or "").strip().upper()
                    label = f"{letter} ({g.min_percentage}-{g.max_percentage})"

                    if getattr(g, "grade_name", ""):
                        label = f"{label} - {g.grade_name}"

                    choices.append((letter, label))
                    self._grade_map[letter] = {
                        "min": g.min_percentage,
                        "max": g.max_percentage,
                        "grade_name": getattr(g, "grade_name", "") or "",
                        "aggregate_point": None,
                    }

        if selected_grade:
            selected_grade = selected_grade.strip().upper()
            existing_values = [choice[0] for choice in choices]

            if selected_grade not in existing_values:
                choices.append((selected_grade, selected_grade))

        return choices

    def clean_grade_letter(self):
        value = self.cleaned_data.get("grade_letter") or ""
        return value.strip().upper()

    def clean(self):
        cleaned = super().clean()

        level = cleaned.get("level")
        grade_letter = (cleaned.get("grade_letter") or "").strip().upper()

        if not grade_letter:
            self.add_error("grade_letter", "Please select a grade.")
            return cleaned

        if grade_letter not in self._grade_map:
            self.add_error(
                "grade_letter",
                f"Selected grade '{grade_letter}' was not found in the active grading scale for this level."
            )
            return cleaned

        cleaned["min_percentage"] = self._grade_map[grade_letter]["min"]
        cleaned["max_percentage"] = self._grade_map[grade_letter]["max"]

        # Duplicate check must happen here so the error appears on the template.
        if self.school and level and grade_letter:
            exists = (
                ReportSubjectCommentRule.objects
                .filter(
                    school=self.school,
                    level=level,
                    grade_letter__iexact=grade_letter,
                )
                .exclude(pk=self.instance.pk)
                .exists()
            )

            if exists:
                self.add_error(
                    "grade_letter",
                    f"A subject comment for grade {grade_letter} already exists for this level. Please edit the existing one instead."
                )

        return cleaned

    def save(self, commit=True):
        obj = super().save(commit=False)

        if self.school and not obj.school_id:
            obj.school = self.school

        grade_letter = (self.cleaned_data.get("grade_letter") or "").strip().upper()
        obj.grade_letter = grade_letter

        if grade_letter in self._grade_map:
            obj.min_percentage = self._grade_map[grade_letter]["min"]
            obj.max_percentage = self._grade_map[grade_letter]["max"]

        # Do NOT call obj.full_clean() here.
        # Form validation already handled the duplicate and grade checks.
        if commit:
            obj.save()

        return obj
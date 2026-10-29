import re

from django import forms

from mainApp.models.calendar import AcademicYear, Term
from mainApp.models.fees_models import FeeCategory
from mainApp.models.models import Class, EducationLevel, Stream
from mainApp.models.student import (
    Parent,
    SchoolAdmissionNumberSetting,
    StudentBulkUploadBatch,
    StudentBulkUploadRow,
)
from mainApp.utils2.student_bulk_upload import IMPORT_FIELDS, normalize_phone

MAX_UPLOAD_MB = 15


def _bootstrap(form):
    for name, f in form.fields.items():
        widget = f.widget
        if isinstance(widget, forms.CheckboxInput):
            widget.attrs.setdefault("class", "form-check-input")
        elif isinstance(widget, forms.ClearableFileInput):
            widget.attrs.setdefault("class", "form-control-file")
        else:
            widget.attrs.setdefault("class", "form-control")


class StudentImportUploadForm(forms.ModelForm):
    """Step 1: the file and how to read it."""

    class Meta:
        model = StudentBulkUploadBatch
        fields = [
            "title", "file", "level", "current_class", "stream",
            "name_order", "allow_missing_dob", "default_residence", "create_parents", "generate_accounts",
        ]
        labels = {
            "level": "Level",
            "current_class": "Class for the whole file",
            "stream": "Stream for the whole file",
            "name_order": "If names are in one column",
            "allow_missing_dob": "Allow learners without a date of birth",
            "default_residence": "Residence when the file has none",
            "create_parents": "Create parents/guardians from the file and link them",
            "generate_accounts": "Create student login accounts after import",
        }
        help_texts = {
            "level": "Optional. Limits class matching to this level.",
            "current_class": "Leave blank if the file has a class column (e.g. a whole-school register).",
            "allow_missing_dob": "Useful for old registers. You can add dates of birth later.",
        }

    def __init__(self, *args, school=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.school = school
        self.fields["file"].widget.attrs["accept"] = ".xlsx"
        self.fields["title"].required = False
        if school is not None:
            self.fields["level"].queryset = school.level.all() if hasattr(school, "level") else EducationLevel.objects.all()
            self.fields["current_class"].queryset = Class.objects.filter(school=school).select_related("level")
            self.fields["stream"].queryset = Stream.objects.filter(class_ref__school=school)
        self.fields["default_residence"].choices = [("", "No default (column required)")] + list(
            self.fields["default_residence"].choices
        )[1:]
        _bootstrap(self)

    def clean_file(self):
        f = self.cleaned_data["file"]
        if f and f.size > MAX_UPLOAD_MB * 1024 * 1024:
            raise forms.ValidationError(f"The file is larger than {MAX_UPLOAD_MB} MB. Split it by class and upload each part.")
        if f and not f.name.lower().endswith(".xlsx"):
            raise forms.ValidationError("Upload an Excel .xlsx file. In Excel use File > Save As > Excel Workbook.")
        return f

    def clean(self):
        data = super().clean()
        level, cls, stream = data.get("level"), data.get("current_class"), data.get("stream")
        if cls and level and cls.level_id != level.id:
            self.add_error("current_class", "This class is not in the selected level.")
        if stream and not cls:
            self.add_error("stream", "Choose the class for this stream.")
        if stream and cls and stream.class_ref_id != cls.id:
            self.add_error("stream", "This stream is not in the selected class.")
        return data


class ColumnMappingForm(forms.Form):
    """Step 2: which file column feeds which field, plus fee balance settings."""

    sheet_name = forms.ChoiceField(label="Sheet")
    header_row = forms.IntegerField(label="Header row", min_value=1, max_value=50)
    fee_category = forms.ModelChoiceField(queryset=FeeCategory.objects.none(), required=False,
                                          label="Fee category for balances")
    balance_academic_year = forms.ModelChoiceField(queryset=AcademicYear.objects.all(), required=False,
                                                   label="Academic year for balances")
    balance_term = forms.ModelChoiceField(queryset=Term.objects.none(), required=False, label="Term for balances")

    NOT_IN_FILE = ""

    def __init__(self, *args, batch, headers, sheet_names, **kwargs):
        super().__init__(*args, **kwargs)
        self.batch = batch
        self.fields["sheet_name"].choices = [(s, s) for s in sheet_names]
        choices = [(self.NOT_IN_FILE, "Not in file")] + [(h, h) for h in headers]
        self.groups = {}
        for f in IMPORT_FIELDS:
            name = f"map_{f.key}"
            self.fields[name] = forms.ChoiceField(choices=choices, required=False, label=f.label, help_text=f.help)
            self.groups.setdefault(f.group, []).append(name)
        school = batch.school
        self.fields["fee_category"].queryset = FeeCategory.objects.filter(school=school, is_active=True)
        self.fields["balance_term"].queryset = Term.objects.select_related("academic_year").order_by(
            "-academic_year__year", "order")
        if not self.is_bound:
            self.initial.setdefault("balance_academic_year", getattr(school, "academic_year_id", None))
            self.initial.setdefault("balance_term", getattr(school, "current_term_id", None))
        _bootstrap(self)

    def grouped_fields(self):
        return [(group, [self[n] for n in names]) for group, names in self.groups.items()]

    def mapping(self):
        return {f.key: self.cleaned_data.get(f"map_{f.key}") for f in IMPORT_FIELDS
                if self.cleaned_data.get(f"map_{f.key}")}

    def clean(self):
        data = super().clean()
        term, year = data.get("balance_term"), data.get("balance_academic_year")
        if term and year and term.academic_year_id != year.id:
            self.add_error("balance_term", "This term is not in the selected academic year.")
        return data


class StudentImportRowForm(forms.ModelForm):
    """Fix one row on the review page without re-uploading the file."""

    resolved_class = forms.ModelChoiceField(queryset=Class.objects.none(), label="Class")
    resolved_stream = forms.ModelChoiceField(queryset=Stream.objects.none(), required=False, label="Stream")
    parent1_relationship = forms.ChoiceField(choices=[("", "---------")] + Parent.RELATIONSHIP_CHOICES,
                                             required=False, label="Parent 1 relationship")
    parent2_relationship = forms.ChoiceField(choices=[("", "---------")] + Parent.RELATIONSHIP_CHOICES,
                                             required=False, label="Parent 2 relationship")

    class Meta:
        model = StudentBulkUploadRow
        fields = [
            "first_name", "middle_name", "last_name", "gender", "date_of_birth", "residence",
            "resolved_class", "resolved_stream", "admission_number", "admission_date", "lin", "uneb_index_number",
            "parent1_name", "parent1_phone", "parent1_relationship", "parent1_email",
            "parent2_name", "parent2_phone", "parent2_relationship", "parent2_email",
            "emergency_contact_name", "emergency_contact_phone", "emergency_contact_relationship",
            "nationality", "religion", "place_of_birth", "previous_school", "previous_class", "opening_balance",
        ]
        widgets = {
            "date_of_birth": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "admission_date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
        }
        labels = {"opening_balance": "Fees balance (negative = paid in advance)"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        school = self.instance.batch.school
        classes = Class.objects.filter(school=school).select_related("level")
        if self.instance.batch.level_id:
            classes = classes.filter(level_id=self.instance.batch.level_id)
        self.fields["resolved_class"].queryset = classes
        self.fields["resolved_stream"].queryset = Stream.objects.filter(class_ref__school=school).select_related("class_ref")
        self.fields["gender"].required = True
        self.fields["residence"].required = True
        self.fields["first_name"].required = True
        self.fields["last_name"].required = True
        _bootstrap(self)

    def _clean_phone(self, name):
        value = self.cleaned_data.get(name)
        primary, _alt, err = normalize_phone(value)
        if err:
            raise forms.ValidationError(err)
        return primary

    def clean_parent1_phone(self):
        return self._clean_phone("parent1_phone")

    def clean_parent2_phone(self):
        return self._clean_phone("parent2_phone")

    def clean_emergency_contact_phone(self):
        return self._clean_phone("emergency_contact_phone")

    def clean(self):
        data = super().clean()
        cls, stream = data.get("resolved_class"), data.get("resolved_stream")
        if stream and cls and stream.class_ref_id != cls.id:
            self.add_error("resolved_stream", "This stream is not in the selected class.")
        return data

    def save(self, commit=True):
        row = super().save(commit=False)
        # Keep the text columns in line with the chosen class so a re-check finds the same class.
        row.class_name = row.resolved_class.name if row.resolved_class else ""
        row.stream_name = row.resolved_stream.name if row.resolved_stream else ""
        if commit:
            row.save()
        return row


class AdmissionNumberSettingForm(forms.ModelForm):
    class Meta:
        model = SchoolAdmissionNumberSetting
        fields = ["pattern", "sequence_padding"]
        labels = {"pattern": "Admission number format", "sequence_padding": "Digits in the running number"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["sequence_padding"].widget.attrs.update({"min": 1, "max": 8})
        _bootstrap(self)

    def clean_pattern(self):
        pattern = (self.cleaned_data.get("pattern") or "").strip()
        if pattern.count("{SEQ}") != 1:
            raise forms.ValidationError("The format must contain {SEQ} exactly once.")
        unknown = set(re.findall(r"\{([^}]*)\}", pattern)) - {"SEQ", "YYYY", "YY", "SCHOOL"}
        if unknown:
            raise forms.ValidationError(f"Unknown code(s): {', '.join('{' + u + '}' for u in sorted(unknown))}.")
        if len(pattern) > 40:
            raise forms.ValidationError("Keep the format under 40 characters.")
        return pattern

    def clean_sequence_padding(self):
        value = self.cleaned_data.get("sequence_padding")
        if value is None or not 1 <= value <= 8:
            raise forms.ValidationError("Use between 1 and 8 digits.")
        return value

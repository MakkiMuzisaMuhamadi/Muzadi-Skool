from django import forms
from django.forms import inlineformset_factory
from django.utils import timezone

from mainApp.models.models import Class, Stream
from mainApp.models.calendar import AcademicYear, Term
from mainApp.models.student import Student
from mainApp.models.fees_models import (
    MANUAL_PAYMENT_METHOD_CHOICES,
    FeeCollectionSetting,
    FeeCategory,
    FeeStructure,
    FeeStructureItem,
    StudentFeePayment,
    StudentFeeAdjustment,
    StudentFeeOpeningBalance,
)

BOOTSTRAP_INPUT = "form-control"
BOOTSTRAP_SELECT = "form-control"
BOOTSTRAP_TEXTAREA = "form-control"


class FeeCategoryForm(forms.ModelForm):
    class Meta:
        model = FeeCategory
        fields = [
            "name",
            "code",
            "description",
            "is_mandatory",
            "is_active",
            "priority",
        ]
        widgets = {
            "name": forms.TextInput(attrs={"class": BOOTSTRAP_INPUT}),
            "code": forms.TextInput(attrs={"class": BOOTSTRAP_INPUT}),
            "description": forms.Textarea(attrs={"class": BOOTSTRAP_TEXTAREA, "rows": 2}),
            "is_mandatory": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "priority": forms.NumberInput(attrs={"class": BOOTSTRAP_INPUT, "min": 1}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["name"].required = True
        self.fields["priority"].required = True
        self.fields["code"].required = False
        self.fields["description"].required = False


class FeeStructureForm(forms.ModelForm):
    class Meta:
        model = FeeStructure
        fields = [
            "academic_year",
            "term",
            "class_ref",
            "stream",
            "residence",
            "apply_mode",
            "name",
            "notes",
            "is_active",
        ]
        widgets = {
            "academic_year": forms.Select(attrs={"class": BOOTSTRAP_SELECT, "id": "id_academic_year"}),
            "term": forms.Select(attrs={"class": BOOTSTRAP_SELECT, "id": "id_term"}),
            "class_ref": forms.Select(attrs={"class": BOOTSTRAP_SELECT, "id": "id_class_ref"}),
            "stream": forms.Select(attrs={"class": BOOTSTRAP_SELECT, "id": "id_stream"}),
            "residence": forms.Select(attrs={"class": BOOTSTRAP_SELECT}),
            "apply_mode": forms.Select(attrs={"class": BOOTSTRAP_SELECT}),
            "name": forms.TextInput(attrs={"class": BOOTSTRAP_INPUT}),
            "notes": forms.Textarea(attrs={"class": BOOTSTRAP_TEXTAREA, "rows": 2}),
            "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }

    def __init__(self, *args, **kwargs):
        school = kwargs.pop("school", None)
        super().__init__(*args, **kwargs)
        self.school = school

        self.fields["stream"].required = False
        self.fields["residence"].required = False
        self.fields["residence"].help_text = "Leave empty. Use 'Apply to' to target boarders or day scholars."
        self.fields["name"].required = False
        self.fields["notes"].required = False
        self.fields["term"].required = True

        self.fields["academic_year"].queryset = AcademicYear.objects.all().order_by("-start_date")
        self.fields["term"].queryset = Term.objects.none()
        self.fields["class_ref"].queryset = Class.objects.none()
        self.fields["stream"].queryset = Stream.objects.none()

        if school:
            self.fields["class_ref"].queryset = Class.objects.filter(
                school=school
            ).select_related("level").order_by("level__order", "name")

        selected_year = None
        selected_class = None

        if self.data.get("academic_year"):
            try:
                selected_year = int(self.data.get("academic_year"))
            except (TypeError, ValueError):
                selected_year = None
        elif self.instance and getattr(self.instance, "academic_year_id", None):
            selected_year = self.instance.academic_year_id

        if selected_year:
            self.fields["term"].queryset = Term.objects.filter(
                academic_year_id=selected_year
            ).order_by("order", "name")

        if self.data.get("class_ref"):
            try:
                selected_class = int(self.data.get("class_ref"))
            except (TypeError, ValueError):
                selected_class = None
        elif self.instance and getattr(self.instance, "class_ref_id", None):
            selected_class = self.instance.class_ref_id

        if selected_class:
            self.fields["stream"].queryset = Stream.objects.filter(
                class_ref_id=selected_class
            ).order_by("name")

    def clean(self):
        cleaned = super().clean()
        class_ref = cleaned.get("class_ref")
        stream = cleaned.get("stream")
        academic_year = cleaned.get("academic_year")
        term = cleaned.get("term")

        if term and academic_year and term.academic_year_id != academic_year.id:
            self.add_error("term", "Selected term does not belong to the selected academic year.")

        if stream and class_ref and stream.class_ref_id != class_ref.id:
            self.add_error("stream", "Selected stream does not belong to the selected class.")

        residence = cleaned.get("residence") or None
        mode = cleaned.get("apply_mode")
        if residence and mode in ("BOARDING_ONLY", "DAY_ONLY"):
            wanted = "BOARDING" if mode == "BOARDING_ONLY" else "DAY"
            if residence != wanted:
                self.add_error("residence", "This contradicts 'Apply to', so no student would be billed. "
                                            "Leave residence empty.")

        # PostgreSQL does not treat two empty streams as equal, so check duplicates here.
        if self.school and academic_year and term and class_ref and mode:
            dup = FeeStructure.objects.filter(
                school=self.school, academic_year=academic_year, term=term, class_ref=class_ref,
                apply_mode=mode, stream=stream, residence=residence)
            if stream is None:
                dup = FeeStructure.objects.filter(
                    school=self.school, academic_year=academic_year, term=term, class_ref=class_ref,
                    apply_mode=mode, stream__isnull=True)
                dup = dup.filter(residence__isnull=True) if residence is None else dup.filter(residence=residence)
            if self.instance.pk:
                dup = dup.exclude(pk=self.instance.pk)
            if dup.exists():
                raise forms.ValidationError(
                    "A fee structure for this class, term and 'Apply to' already exists. Edit that one instead.")
        return cleaned


class FeeStructureItemForm(forms.ModelForm):
    class Meta:
        model = FeeStructureItem
        fields = ["fee_category", "amount", "notes"]
        widgets = {
            "fee_category": forms.Select(attrs={"class": BOOTSTRAP_SELECT}),
            "amount": forms.NumberInput(attrs={"class": BOOTSTRAP_INPUT, "step": "0.01", "min": "0"}),
            "notes": forms.TextInput(attrs={"class": BOOTSTRAP_INPUT}),
        }

    def __init__(self, *args, **kwargs):
        school = kwargs.pop("school", None)
        super().__init__(*args, **kwargs)
        self.fields["notes"].required = False

        if school:
            self.fields["fee_category"].queryset = FeeCategory.objects.filter(
                school=school,
                is_active=True
            ).order_by("priority", "name")


FeeStructureItemFormSet = inlineformset_factory(
    FeeStructure,
    FeeStructureItem,
    form=FeeStructureItemForm,
    extra=1,
    can_delete=True
)


class StudentFeePaymentForm(forms.ModelForm):
    student_search = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={
            "class": BOOTSTRAP_INPUT,
            "placeholder": "Search by admission number or student name"
        })
    )

    class Meta:
        model = StudentFeePayment
        fields = [
            "student",
            "academic_year",
            "term",
            "payment_date",
            "amount",
            "payment_method",
            "reference",
            "notes",
        ]
        widgets = {
            "student": forms.Select(attrs={"class": BOOTSTRAP_SELECT, "id": "id_student"}),
            "academic_year": forms.Select(attrs={"class": BOOTSTRAP_SELECT, "id": "id_academic_year"}),
            "term": forms.Select(attrs={"class": BOOTSTRAP_SELECT, "id": "id_term"}),
            "payment_date": forms.DateInput(attrs={"class": BOOTSTRAP_INPUT, "type": "date"}),
            "amount": forms.NumberInput(attrs={"class": BOOTSTRAP_INPUT, "step": "0.01", "min": "0"}),
            "payment_method": forms.Select(attrs={"class": BOOTSTRAP_SELECT}),
            "reference": forms.TextInput(attrs={"class": BOOTSTRAP_INPUT}),
            "notes": forms.Textarea(attrs={"class": BOOTSTRAP_TEXTAREA, "rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        school = kwargs.pop("school", None)
        super().__init__(*args, **kwargs)

        self.fields["student"].queryset = Student.objects.none()
        self.fields["academic_year"].queryset = AcademicYear.objects.all().order_by("-start_date")
        self.fields["term"].queryset = Term.objects.none()

        if school:
            self.fields["student"].queryset = Student.objects.filter(
                school=school,
                is_active=True
            ).order_by("admission_number", "name")

        selected_year = None
        if self.data.get("academic_year"):
            try:
                selected_year = int(self.data.get("academic_year"))
            except (TypeError, ValueError):
                selected_year = None
        elif self.instance and getattr(self.instance, "academic_year_id", None):
            selected_year = self.instance.academic_year_id
        elif self.initial.get("academic_year"):
            try:
                selected_year = int(self.initial.get("academic_year"))
            except (TypeError, ValueError):
                selected_year = None

        if selected_year:
            self.fields["term"].queryset = Term.objects.filter(
                academic_year_id=selected_year
            ).order_by("order", "name")

        self.fields["student"].widget = forms.HiddenInput()
        self.fields["reference"].required = False
        self.fields["notes"].required = False
        self.fields["payment_date"].initial = timezone.localdate()
        self.fields["payment_method"].choices = MANUAL_PAYMENT_METHOD_CHOICES
        self.fields["term"].help_text = ("The term the money is received in. "
                                         "It always clears the oldest unpaid fees first.")

        # Default to the term we are in now.
        if school and not self.is_bound and not self.initial.get("term"):
            from mainApp.fee_services import resolve_term_for_date
            term = resolve_term_for_date(school)
            if term:
                self.initial.setdefault("academic_year", term.academic_year_id)
                self.initial["term"] = term.pk
                self.fields["term"].queryset = Term.objects.filter(
                    academic_year_id=term.academic_year_id).order_by("order", "name")

    def clean(self):
        cleaned = super().clean()
        academic_year = cleaned.get("academic_year")
        term = cleaned.get("term")

        if term and academic_year and term.academic_year_id != academic_year.id:
            self.add_error("term", "Selected term does not belong to the selected academic year.")

        method = cleaned.get("payment_method")
        if method in ("BANK", "MOBILE", "CHEQUE") and not (cleaned.get("reference") or "").strip():
            self.add_error("reference", "Enter the bank slip, cheque or mobile money transaction number.")
        return cleaned

    def clean_amount(self):
        amount = self.cleaned_data.get("amount")
        if amount is not None and amount <= 0:
            raise forms.ValidationError("Amount must be greater than zero.")
        return amount


class StudentFeeAdjustmentForm(forms.ModelForm):
    class Meta:
        model = StudentFeeAdjustment
        fields = ["adjustment_type", "amount", "reason"]
        widgets = {
            "adjustment_type": forms.Select(attrs={"class": BOOTSTRAP_SELECT}),
            "amount": forms.NumberInput(attrs={"class": BOOTSTRAP_INPUT, "step": "0.01", "min": "0"}),
            "reason": forms.Textarea(attrs={"class": BOOTSTRAP_TEXTAREA, "rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        self.charge = kwargs.pop("charge", None)
        super().__init__(*args, **kwargs)
        self.fields["reason"].required = True

    def clean_amount(self):
        amount = self.cleaned_data.get("amount")
        if amount is not None and amount <= 0:
            raise forms.ValidationError("Adjustment amount must be greater than zero.")
        return amount

    def clean(self):
        cleaned = super().clean()
        amount, kind = cleaned.get("amount"), cleaned.get("adjustment_type")
        if self.charge and amount and kind in ("DISCOUNT", "WAIVER") and amount > self.charge.balance:
            self.add_error("amount", f"Cannot be more than the unpaid balance (UGX {self.charge.balance:,.0f}).")
        return cleaned


class StudentFeeOpeningBalanceForm(forms.ModelForm):
    class Meta:
        model = StudentFeeOpeningBalance
        fields = [
            "student",
            "academic_year",
            "term",
            "fee_category",
            "amount",
            "notes",
        ]
        widgets = {
            "student": forms.Select(attrs={"class": BOOTSTRAP_SELECT, "id": "id_student"}),
            "academic_year": forms.Select(attrs={"class": BOOTSTRAP_SELECT, "id": "id_academic_year"}),
            "term": forms.Select(attrs={"class": BOOTSTRAP_SELECT, "id": "id_term"}),
            "fee_category": forms.Select(attrs={"class": BOOTSTRAP_SELECT}),
            "amount": forms.NumberInput(attrs={"class": BOOTSTRAP_INPUT, "step": "0.01", "min": "0"}),
            "notes": forms.Textarea(attrs={"class": BOOTSTRAP_TEXTAREA, "rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        school = kwargs.pop("school", None)
        super().__init__(*args, **kwargs)

        self.fields["notes"].required = False
        self.fields["term"].required = True
        self.fields["academic_year"].required = True
        self.fields["term"].help_text = "Usually the first term you are using the system in."

        self.fields["academic_year"].queryset = AcademicYear.objects.all().order_by("-start_date")
        self.fields["term"].queryset = Term.objects.none()

        if school:
            self.fields["student"].queryset = Student.objects.filter(
                school=school,
                is_active=True
            ).order_by("admission_number", "name")
            self.fields["fee_category"].queryset = FeeCategory.objects.filter(
                school=school,
                is_active=True
            ).order_by("priority", "name")

        selected_year = None
        if self.data.get("academic_year"):
            try:
                selected_year = int(self.data.get("academic_year"))
            except (TypeError, ValueError):
                selected_year = None
        elif self.instance and getattr(self.instance, "academic_year_id", None):
            selected_year = self.instance.academic_year_id

        if selected_year:
            self.fields["term"].queryset = Term.objects.filter(
                academic_year_id=selected_year
            ).order_by("order", "name")

    def clean(self):
        cleaned = super().clean()
        academic_year = cleaned.get("academic_year")
        term = cleaned.get("term")

        if term and academic_year and term.academic_year_id != academic_year.id:
            self.add_error("term", "Selected term does not belong to the selected academic year.")

        return cleaned

    def clean_amount(self):
        amount = self.cleaned_data.get("amount")
        if amount is not None and amount <= 0:
            raise forms.ValidationError("Old fee balance amount must be greater than zero.")
        return amount


class StudentManualChargeForm(forms.Form):
    student = forms.ModelChoiceField(
        queryset=Student.objects.none(),
        widget=forms.Select(attrs={
            "class": BOOTSTRAP_SELECT,
            "id": "id_student",
        })
    )
    academic_year = forms.ModelChoiceField(
        queryset=AcademicYear.objects.none(),
        widget=forms.Select(attrs={
            "class": BOOTSTRAP_SELECT,
            "id": "id_academic_year",
        })
    )
    term = forms.ModelChoiceField(
        queryset=Term.objects.none(),
        widget=forms.Select(attrs={
            "class": BOOTSTRAP_SELECT,
            "id": "id_term",
        })
    )
    fee_category = forms.ModelChoiceField(
        queryset=FeeCategory.objects.none(),
        widget=forms.Select(attrs={"class": BOOTSTRAP_SELECT})
    )
    amount = forms.DecimalField(
        min_value=0.01,
        decimal_places=2,
        max_digits=12,
        widget=forms.NumberInput(attrs={"class": BOOTSTRAP_INPUT, "step": "0.01"})
    )
    description = forms.CharField(
        widget=forms.TextInput(attrs={"class": BOOTSTRAP_INPUT})
    )
    notes = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"class": BOOTSTRAP_TEXTAREA, "rows": 2})
    )

    def __init__(self, *args, **kwargs):
        school = kwargs.pop("school", None)
        super().__init__(*args, **kwargs)

        self.fields["academic_year"].queryset = AcademicYear.objects.all().order_by("-start_date")
        self.fields["term"].queryset = Term.objects.none()

        if school:
            self.fields["student"].queryset = Student.objects.filter(
                school=school,
                is_active=True
            ).order_by("admission_number", "name")
            self.fields["fee_category"].queryset = FeeCategory.objects.filter(
                school=school,
                is_active=True
            ).order_by("priority", "name")

        selected_year = None
        if self.data.get("academic_year"):
            try:
                selected_year = int(self.data.get("academic_year"))
            except (TypeError, ValueError):
                selected_year = None
        elif self.initial.get("academic_year"):
            try:
                selected_year = int(self.initial.get("academic_year"))
            except (TypeError, ValueError):
                selected_year = None

        if selected_year:
            self.fields["term"].queryset = Term.objects.filter(
                academic_year_id=selected_year
            ).order_by("order", "name")

    def clean(self):
        cleaned = super().clean()
        academic_year = cleaned.get("academic_year")
        term = cleaned.get("term")

        if term and academic_year and term.academic_year_id != academic_year.id:
            self.add_error("term", "Selected term does not belong to the selected academic year.")
        return cleaned


class BulkManualChargeForm(forms.Form):
    academic_year = forms.ModelChoiceField(
        queryset=AcademicYear.objects.none(),
        widget=forms.Select(attrs={
            "class": BOOTSTRAP_SELECT,
            "id": "id_academic_year",
        })
    )
    term = forms.ModelChoiceField(
        queryset=Term.objects.none(),
        widget=forms.Select(attrs={
            "class": BOOTSTRAP_SELECT,
            "id": "id_term",
        })
    )
    class_ref = forms.ModelChoiceField(
        queryset=Class.objects.none(),
        widget=forms.Select(attrs={
            "class": BOOTSTRAP_SELECT,
            "id": "id_class_ref",
        })
    )
    stream = forms.ModelChoiceField(
        queryset=Stream.objects.none(),
        required=False,
        widget=forms.Select(attrs={
            "class": BOOTSTRAP_SELECT,
            "id": "id_stream",
        })
    )
    fee_category = forms.ModelChoiceField(
        queryset=FeeCategory.objects.none(),
        widget=forms.Select(attrs={"class": BOOTSTRAP_SELECT})
    )
    amount = forms.DecimalField(
        min_value=0.01,
        decimal_places=2,
        max_digits=12,
        widget=forms.NumberInput(attrs={"class": BOOTSTRAP_INPUT, "step": "0.01"})
    )
    description = forms.CharField(
        widget=forms.TextInput(attrs={"class": BOOTSTRAP_INPUT})
    )
    notes = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"class": BOOTSTRAP_TEXTAREA, "rows": 2})
    )
    skip_if_same_description_exists = forms.BooleanField(
        required=False,
        initial=True,
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"})
    )

    def __init__(self, *args, **kwargs):
        school = kwargs.pop("school", None)
        super().__init__(*args, **kwargs)

        self.fields["academic_year"].queryset = AcademicYear.objects.all().order_by("-start_date")
        self.fields["term"].queryset = Term.objects.none()
        self.fields["class_ref"].queryset = Class.objects.none()
        self.fields["stream"].queryset = Stream.objects.none()

        if school:
            self.fields["class_ref"].queryset = Class.objects.filter(
                school=school
            ).select_related("level").order_by("level__order", "name")
            self.fields["fee_category"].queryset = FeeCategory.objects.filter(
                school=school,
                is_active=True
            ).order_by("priority", "name")

        selected_year = None
        selected_class = None

        if self.data.get("academic_year"):
            try:
                selected_year = int(self.data.get("academic_year"))
            except (TypeError, ValueError):
                selected_year = None
        elif self.initial.get("academic_year"):
            try:
                selected_year = int(self.initial.get("academic_year"))
            except (TypeError, ValueError):
                selected_year = None

        if selected_year:
            self.fields["term"].queryset = Term.objects.filter(
                academic_year_id=selected_year
            ).order_by("order", "name")

        if self.data.get("class_ref"):
            try:
                selected_class = int(self.data.get("class_ref"))
            except (TypeError, ValueError):
                selected_class = None
        elif self.initial.get("class_ref"):
            try:
                selected_class = int(self.initial.get("class_ref"))
            except (TypeError, ValueError):
                selected_class = None

        if selected_class:
            self.fields["stream"].queryset = Stream.objects.filter(
                class_ref_id=selected_class
            ).order_by("name")

    def clean(self):
        cleaned = super().clean()
        academic_year = cleaned.get("academic_year")
        term = cleaned.get("term")
        class_ref = cleaned.get("class_ref")
        stream = cleaned.get("stream")

        if term and academic_year and term.academic_year_id != academic_year.id:
            self.add_error("term", "Selected term does not belong to the selected academic year.")

        if stream and class_ref and stream.class_ref_id != class_ref.id:
            self.add_error("stream", "Selected stream does not belong to the selected class.")

        return cleaned


# =============================================================== new forms ==

class PaymentReversalForm(forms.Form):
    reason = forms.CharField(
        max_length=255,
        widget=forms.Textarea(attrs={"class": BOOTSTRAP_TEXTAREA, "rows": 3,
                                     "placeholder": "e.g. Typed 500,000 instead of 50,000"}))
    confirm = forms.BooleanField(
        label="I understand this cancels the receipt and puts the money back on the student's balance.",
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}))


class AdjustmentDecisionForm(forms.Form):
    note = forms.CharField(required=False, max_length=255,
                           widget=forms.TextInput(attrs={"class": BOOTSTRAP_INPUT, "placeholder": "Note (optional)"}))


class AdjustmentReversalForm(forms.Form):
    reason = forms.CharField(max_length=255, widget=forms.TextInput(attrs={"class": BOOTSTRAP_INPUT}))


class WaiveTermForm(forms.Form):
    term = forms.ModelChoiceField(queryset=Term.objects.none(),
                                  widget=forms.Select(attrs={"class": BOOTSTRAP_SELECT}),
                                  help_text="Unpaid fees in this term and every later term are waived.")
    reason = forms.CharField(max_length=255, widget=forms.TextInput(
        attrs={"class": BOOTSTRAP_INPUT, "placeholder": "e.g. Left school on 12 June 2026"}))

    def __init__(self, *args, **kwargs):
        student = kwargs.pop("student", None)
        super().__init__(*args, **kwargs)
        qs = Term.objects.select_related("academic_year").order_by("-start_date")
        if student is not None:
            qs = qs.filter(student_fee_charges__student=student).distinct()
        self.fields["term"].queryset = qs
        self.fields["term"].label_from_instance = lambda t: f"{t.name} {t.academic_year.year}"


class FeeCollectionSettingForm(forms.ModelForm):
    class Meta:
        model = FeeCollectionSetting
        fields = ["clearance_percentage", "waiver_approval_threshold", "auto_apply_carry_forward_credit"]
        widgets = {
            "clearance_percentage": forms.NumberInput(attrs={"class": BOOTSTRAP_INPUT, "min": 0, "max": 100}),
            "waiver_approval_threshold": forms.NumberInput(attrs={"class": BOOTSTRAP_INPUT, "min": 0, "step": "1000"}),
            "auto_apply_carry_forward_credit": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }

    def clean_clearance_percentage(self):
        v = self.cleaned_data["clearance_percentage"]
        if v > 100:
            raise forms.ValidationError("Use a value from 0 to 100.")
        return v


class DefaulterReminderForm(forms.Form):
    message = forms.CharField(
        max_length=300,
        initial="Dear {parent}, {student}'s outstanding school fees balance is UGX {balance}. "
                "Kindly clear it. {school}",
        widget=forms.Textarea(attrs={"class": BOOTSTRAP_TEXTAREA, "rows": 3}),
        help_text="You can use {parent}, {student}, {balance} and {school}.")

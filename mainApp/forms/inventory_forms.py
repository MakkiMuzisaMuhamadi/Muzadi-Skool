# mainApp/forms/inventory_forms.py
from __future__ import annotations
from django import forms
from django.utils.text import slugify

from mainApp.models.inventory_models import (
    InventoryCategory,
    InventoryItem,
    InventoryUnit,
    InventoryLocation,
)
from mainApp.models.student import Student

BOOTSTRAP_INPUT = "form-control"
BOOTSTRAP_SELECT = "form-control"
BOOTSTRAP_TEXTAREA = "form-control"
class SchoolScopedFormMixin:
    def __init__(self, *args, **kwargs):
        self.school = kwargs.pop("school", None)
        super().__init__(*args, **kwargs)

        # ✅ bootstrap classes
        for name, field in self.fields.items():
            w = field.widget
            if getattr(w, "input_type", None) in ("checkbox", "radio"):
                continue
            css = w.attrs.get("class", "")
            if isinstance(w, forms.Select):
                w.attrs["class"] = f"{css} {BOOTSTRAP_SELECT}".strip()
            elif isinstance(w, forms.Textarea):
                w.attrs["class"] = f"{css} {BOOTSTRAP_TEXTAREA}".strip()
            else:
                w.attrs["class"] = f"{css} {BOOTSTRAP_INPUT}".strip()

        # ✅ school scoping
        if self.school:
            for fname in ["category", "item", "current_location"]:
                if fname in self.fields and hasattr(self.fields[fname], "queryset"):
                    self.fields[fname].queryset = self.fields[fname].queryset.filter(school=self.school)

class InventoryCategoryForm(SchoolScopedFormMixin, forms.ModelForm):
    class Meta:
        model = InventoryCategory
        fields = ["type", "name", "description", "is_active"]


class InventoryLocationForm(SchoolScopedFormMixin, forms.ModelForm):
    class Meta:
        model = InventoryLocation
        fields = ["name", "description", "is_active", "order"]


class InventoryItemForm(SchoolScopedFormMixin, forms.ModelForm):
    class Meta:
        model = InventoryItem
        fields = [
            "category", "name", "description",
            "isbn", "author", "publisher", "edition", "subject_hint",
            "brand", "model",
            "is_serialized", "is_active",
        ]


class InventoryUnitForm(SchoolScopedFormMixin, forms.ModelForm):
    class Meta:
        model = InventoryUnit
        fields = ["item", "tag_code", "serial_number", "status", "condition", "current_location", "notes"]


class BulkAddUnitsForm(forms.Form):
    item = forms.ModelChoiceField(queryset=InventoryItem.objects.none())
    prefix = forms.CharField(max_length=30, required=False, help_text="e.g. LIB-BIO or AST-ICT")
    start_number = forms.IntegerField(min_value=1)
    quantity = forms.IntegerField(min_value=1, max_value=500)
    location = forms.ModelChoiceField(queryset=InventoryLocation.objects.none(), required=False)

    def __init__(self, *args, **kwargs):
        self.school = kwargs.pop("school", None)
        super().__init__(*args, **kwargs)
        if self.school:
            self.fields["item"].queryset = InventoryItem.objects.filter(school=self.school, is_active=True)
            self.fields["location"].queryset = InventoryLocation.objects.filter(school=self.school, is_active=True)


class IssueScanForm(forms.Form):
    student = forms.ModelChoiceField(queryset=Student.objects.none())
    tag_code = forms.CharField(max_length=80)

    def __init__(self, *args, **kwargs):
        self.school = kwargs.pop("school", None)
        super().__init__(*args, **kwargs)
        if self.school:
            self.fields["student"].queryset = Student.objects.filter(school=self.school)


class ReturnScanForm(forms.Form):
    tag_code = forms.CharField(max_length=80)
    condition = forms.ChoiceField(
        required=False,
        choices=[("", "— keep current —"), ("NEW", "New"), ("GOOD", "Good"), ("FAIR", "Fair"), ("DAMAGED", "Damaged")],
    )
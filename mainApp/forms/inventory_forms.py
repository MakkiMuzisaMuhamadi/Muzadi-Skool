# mainApp/forms/inventory_forms.py
from __future__ import annotations

from django import forms
from django.contrib.auth.models import User
from django.forms import inlineformset_factory
from django.utils import timezone

from mainApp.models.inventory_models import (
    ClassLoanPolicy,
    GoodsReceipt,
    GoodsReceiptLine,
    InventoryCategory,
    InventoryItem,
    InventoryLocation,
    InventorySettings,
    InventoryUnit,
    PurchaseOrder,
    PurchaseOrderLine,
    Supplier,
)
from mainApp.models.calendar import Term
from mainApp.models.models import Class, Stream
from mainApp.models.student import Student

DATE = forms.DateInput(attrs={"type": "date"})
TEXTAREA = forms.Textarea(attrs={"rows": 2})


def staff_queryset(school):
    return (User.objects.filter(is_active=True, profile__school=school)
            .exclude(profile__role="STUDENT").order_by("first_name", "last_name"))


class StaffChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return obj.get_full_name() or obj.username


class SchoolScopedFormMixin:
    """Pass school=...; every school-owned dropdown is limited to that school, and Bootstrap classes are added."""

    def __init__(self, *args, school=None, **kwargs):
        self.school = school
        super().__init__(*args, **kwargs)
        for name, f in self.fields.items():
            if isinstance(f, forms.ModelChoiceField) and school is not None:
                model = f.queryset.model
                if model is User:
                    f.queryset = staff_queryset(school)
                elif any(fld.name == "school" for fld in model._meta.get_fields()):
                    f.queryset = f.queryset.filter(school=school)
                elif model.__name__ == "Stream":
                    f.queryset = f.queryset.filter(class_ref__school=school)
                if hasattr(model, "is_active") and model is not User and model is not Student:
                    instance_value = getattr(getattr(self, "instance", None), f"{name}_id", None)
                    f.queryset = f.queryset.filter(is_active=True) | f.queryset.filter(pk=instance_value)
            w = f.widget
            if getattr(w, "input_type", None) in ("checkbox", "radio") or isinstance(w, forms.CheckboxInput):
                w.attrs["class"] = (w.attrs.get("class", "") + " form-check-input").strip()
            else:
                w.attrs["class"] = (w.attrs.get("class", "") + " form-control").strip()


# =============================================================================
# Settings & setup
# =============================================================================
class InventorySettingsForm(SchoolScopedFormMixin, forms.ModelForm):
    class Meta:
        model = InventorySettings
        fields = ["student_max_loans", "student_loan_days", "staff_max_loans", "staff_loan_days",
                  "block_if_overdue", "block_staff_if_overdue", "fee_category", "charge_lost_items",
                  "damage_charge_percent", "overdue_fine_per_day"]


class ClassLoanPolicyForm(SchoolScopedFormMixin, forms.ModelForm):
    class Meta:
        model = ClassLoanPolicy
        fields = ["class_ref", "max_loans", "loan_days"]
        labels = {"class_ref": "Class"}

    def clean_class_ref(self):
        c = self.cleaned_data["class_ref"]
        dup = ClassLoanPolicy.objects.filter(school=self.school, class_ref=c).exclude(pk=self.instance.pk)
        if dup.exists():
            raise forms.ValidationError("This class already has its own rule. Edit that one instead.")
        return c


class InventoryCategoryForm(SchoolScopedFormMixin, forms.ModelForm):
    class Meta:
        model = InventoryCategory
        fields = ["type", "name", "description", "is_active"]
        widgets = {"description": TEXTAREA}

    def clean(self):
        data = super().clean()
        if InventoryCategory.objects.filter(school=self.school, type=data.get("type"), name=data.get("name")) \
                .exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError("A category with this type and name already exists.")
        return data


class InventoryLocationForm(SchoolScopedFormMixin, forms.ModelForm):
    class Meta:
        model = InventoryLocation
        fields = ["name", "kind", "custodian", "description", "order", "is_active"]
        widgets = {"description": TEXTAREA}

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        if InventoryLocation.objects.filter(school=self.school, name__iexact=name).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError("A location with this name already exists.")
        return name


class SupplierForm(SchoolScopedFormMixin, forms.ModelForm):
    class Meta:
        model = Supplier
        fields = ["name", "contact_person", "phone", "email", "tin", "address", "notes", "is_active"]
        widgets = {"address": TEXTAREA, "notes": TEXTAREA}

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        if Supplier.objects.filter(school=self.school, name__iexact=name).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError("A supplier with this name already exists.")
        return name


class InventoryItemForm(SchoolScopedFormMixin, forms.ModelForm):
    class Meta:
        model = InventoryItem
        fields = [
            "category", "name", "description",
            "isbn", "author", "publisher", "edition", "subject_hint",
            "brand", "model", "useful_life_years",
            "is_serialized", "tag_prefix", "unit_of_measure", "reorder_level",
            "default_unit_cost", "replacement_cost", "is_active",
        ]
        widgets = {"description": TEXTAREA}
        labels = {"is_serialized": "Track each copy with its own tag"}
        help_texts = {"is_serialized": "Library books are always tagged; consumables are always counted."}

    def __init__(self, *args, allowed_types=None, **kwargs):
        super().__init__(*args, **kwargs)
        if allowed_types is not None:
            self.fields["category"].queryset = self.fields["category"].queryset.filter(type__in=allowed_types)

    def _post_clean(self):
        self.instance.school = self.school
        super()._post_clean()


class InventoryUnitCreateForm(SchoolScopedFormMixin, forms.Form):
    item = forms.ModelChoiceField(queryset=InventoryItem.objects.filter(is_serialized=True))
    tag_code = forms.CharField(max_length=80, required=False, help_text="Leave blank to generate the next tag.")
    serial_number = forms.CharField(max_length=120, required=False)
    condition = forms.ChoiceField(choices=InventoryUnit.CONDITION_CHOICES, initial="NEW")
    location = forms.ModelChoiceField(queryset=InventoryLocation.objects.all(), required=False)
    unit_cost = forms.DecimalField(max_digits=14, decimal_places=2, required=False)
    purchase_date = forms.DateField(required=False, widget=DATE)
    supplier = forms.ModelChoiceField(queryset=Supplier.objects.all(), required=False)
    funding_source = forms.CharField(max_length=120, required=False)
    warranty_expiry = forms.DateField(required=False, widget=DATE)
    notes = forms.CharField(required=False, widget=TEXTAREA)


class InventoryUnitEditForm(SchoolScopedFormMixin, forms.ModelForm):
    """Descriptive details only. Status, holder and location change through actions (audited)."""

    class Meta:
        model = InventoryUnit
        fields = ["serial_number", "condition", "unit_cost", "purchase_date", "supplier",
                  "warranty_expiry", "funding_source", "notes"]
        widgets = {"purchase_date": DATE, "warranty_expiry": DATE, "notes": TEXTAREA}


class BulkAddUnitsForm(SchoolScopedFormMixin, forms.Form):
    item = forms.ModelChoiceField(queryset=InventoryItem.objects.filter(is_serialized=True))
    quantity = forms.IntegerField(min_value=1, max_value=1000)
    prefix = forms.CharField(max_length=20, required=False,
                             help_text="Optional, e.g. LIB-BIO. Tags continue from the last number used.")
    location = forms.ModelChoiceField(queryset=InventoryLocation.objects.all(), required=False)
    condition = forms.ChoiceField(choices=InventoryUnit.CONDITION_CHOICES, initial="NEW")
    unit_cost = forms.DecimalField(max_digits=14, decimal_places=2, required=False)
    purchase_date = forms.DateField(required=False, widget=DATE)
    supplier = forms.ModelChoiceField(queryset=Supplier.objects.all(), required=False)
    funding_source = forms.CharField(max_length=120, required=False)


# =============================================================================
# Unit actions
# =============================================================================
class RemarkForm(SchoolScopedFormMixin, forms.Form):
    remark = forms.CharField(required=False, widget=TEXTAREA)


class AllocateUnitForm(SchoolScopedFormMixin, forms.Form):
    staff = StaffChoiceField(queryset=User.objects.all(), required=False, label="Staff member (with login)")
    staff_name = forms.CharField(max_length=200, required=False, label="…or staff name",
                                 help_text="For staff without a login, e.g. the matron or a driver.")
    location = forms.ModelChoiceField(queryset=InventoryLocation.objects.all(), required=False,
                                      label="Move to location")
    remark = forms.CharField(required=False, widget=TEXTAREA)

    def clean(self):
        data = super().clean()
        if not (data.get("staff") or data.get("staff_name") or data.get("location")):
            raise forms.ValidationError("Choose a staff member or a location.")
        return data


class IssueToStaffForm(SchoolScopedFormMixin, forms.Form):
    staff = StaffChoiceField(queryset=User.objects.all(), required=False, label="Staff member (with login)")
    staff_name = forms.CharField(max_length=200, required=False, label="…or staff name")
    due_date = forms.DateField(required=False, widget=DATE, help_text="Blank uses the staff loan period.")
    remark = forms.CharField(required=False, widget=TEXTAREA)

    def clean(self):
        data = super().clean()
        if not (data.get("staff") or data.get("staff_name")):
            raise forms.ValidationError("Choose the staff member.")
        return data


class ReturnUnitForm(SchoolScopedFormMixin, forms.Form):
    condition = forms.ChoiceField(
        required=False, choices=[("", "Keep current condition")] + InventoryUnit.CONDITION_CHOICES)
    return_to = forms.ModelChoiceField(queryset=InventoryLocation.objects.all(), required=False,
                                       label="Return to location")
    charge_damage = forms.BooleanField(required=False, label="Bill the student if damaged")
    remark = forms.CharField(required=False, widget=TEXTAREA)


class TransferUnitForm(SchoolScopedFormMixin, forms.Form):
    to_location = forms.ModelChoiceField(queryset=InventoryLocation.objects.all(), label="Move to")
    remark = forms.CharField(required=False, widget=TEXTAREA)


class RepairCompleteForm(SchoolScopedFormMixin, forms.Form):
    condition = forms.ChoiceField(choices=InventoryUnit.CONDITION_CHOICES, initial="GOOD")
    remark = forms.CharField(required=False, widget=TEXTAREA)


class MarkLostForm(SchoolScopedFormMixin, forms.Form):
    charge_holder = forms.BooleanField(required=False, initial=True,
                                       label="Bill the student who held it (replacement cost)")
    remark = forms.CharField(required=False, widget=TEXTAREA)


class MarkFoundForm(SchoolScopedFormMixin, forms.Form):
    location = forms.ModelChoiceField(queryset=InventoryLocation.objects.all(), required=False)
    condition = forms.ChoiceField(required=False,
                                  choices=[("", "Keep current condition")] + InventoryUnit.CONDITION_CHOICES)
    remark = forms.CharField(required=False, widget=TEXTAREA)


class DisposeUnitForm(SchoolScopedFormMixin, forms.Form):
    reason = forms.CharField(widget=TEXTAREA, help_text="e.g. Beyond repair; approved by the board on 12 May.")


# Legacy scan screen (kept for the non-JS fallback)
class IssueScanForm(SchoolScopedFormMixin, forms.Form):
    student = forms.ModelChoiceField(queryset=Student.objects.all())
    tag_code = forms.CharField(max_length=80)


class ReturnScanForm(forms.Form):
    tag_code = forms.CharField(max_length=80)
    condition = forms.ChoiceField(
        required=False, choices=[("", "— keep current —")] + InventoryUnit.CONDITION_CHOICES)


# =============================================================================
# Term textbooks
# =============================================================================
class ClassIssueForm(SchoolScopedFormMixin, forms.Form):
    item = forms.ModelChoiceField(queryset=InventoryItem.objects.filter(is_serialized=True),
                                  label="Book / item")
    class_ref = forms.ModelChoiceField(queryset=Class.objects.all(), label="Class")
    stream = forms.ModelChoiceField(queryset=Stream.objects.all(), required=False,
                                    help_text="Blank = whole class.")
    term = forms.ModelChoiceField(queryset=Term.objects.order_by("-start_date"), required=False)
    due_date = forms.DateField(widget=DATE, help_text="Usually the last day of term.")
    from_location = forms.ModelChoiceField(queryset=InventoryLocation.objects.all(), required=False,
                                           help_text="Only take copies kept here (optional).")
    notes = forms.CharField(required=False, widget=TEXTAREA)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["class_ref"].queryset = self.fields["class_ref"].queryset.order_by("name")
        self.fields["stream"].queryset = self.fields["stream"].queryset.order_by("class_ref__name", "name")

    def clean(self):
        data = super().clean()
        stream, class_ref = data.get("stream"), data.get("class_ref")
        if stream and class_ref and stream.class_ref_id != class_ref.pk:
            self.add_error("stream", "That stream is not in the chosen class.")
        if data.get("due_date") and data["due_date"] < timezone.localdate():
            self.add_error("due_date", "The due date cannot be in the past.")
        return data


# =============================================================================
# Counted stock
# =============================================================================
QTY_FIELD = {"max_digits": 12, "decimal_places": 2, "min_value": 0}


def counted_items(school):
    return InventoryItem.objects.filter(school=school, is_serialized=False, is_active=True).order_by("name")


class _StockForm(SchoolScopedFormMixin, forms.Form):
    item = forms.ModelChoiceField(queryset=InventoryItem.objects.filter(is_serialized=False))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["item"].queryset = counted_items(self.school)


class StockReceiveForm(_StockForm):
    location = forms.ModelChoiceField(queryset=InventoryLocation.objects.all(), label="Into location")
    quantity = forms.DecimalField(**QTY_FIELD)
    unit_cost = forms.DecimalField(max_digits=14, decimal_places=2, required=False,
                                   help_text="Blank uses the item's default cost.")
    supplier = forms.ModelChoiceField(queryset=Supplier.objects.all(), required=False)
    reference = forms.CharField(max_length=60, required=False, help_text="Delivery note / receipt no.")
    occurred_on = forms.DateField(initial=timezone.localdate, widget=DATE, label="Date")
    remark = forms.CharField(required=False, widget=TEXTAREA)


class StockIssueForm(_StockForm):
    location = forms.ModelChoiceField(queryset=InventoryLocation.objects.all(), label="From location")
    quantity = forms.DecimalField(**QTY_FIELD)
    staff = StaffChoiceField(queryset=User.objects.all(), required=False, label="To staff member")
    department = forms.CharField(max_length=120, required=False, label="…or department",
                                 help_text="e.g. Kitchen, Science Dept, S3 Blue")
    occurred_on = forms.DateField(initial=timezone.localdate, widget=DATE, label="Date")
    remark = forms.CharField(required=False, widget=TEXTAREA)

    def clean(self):
        data = super().clean()
        if not (data.get("staff") or (data.get("department") or "").strip()):
            raise forms.ValidationError("Say who received the stock: a staff member or a department.")
        return data


class StockReturnForm(_StockForm):
    location = forms.ModelChoiceField(queryset=InventoryLocation.objects.all(), label="Back into location")
    quantity = forms.DecimalField(**QTY_FIELD)
    remark = forms.CharField(required=False, widget=TEXTAREA)


class StockTransferForm(_StockForm):
    from_location = forms.ModelChoiceField(queryset=InventoryLocation.objects.all())
    to_location = forms.ModelChoiceField(queryset=InventoryLocation.objects.all())
    quantity = forms.DecimalField(**QTY_FIELD)
    remark = forms.CharField(required=False, widget=TEXTAREA)

    def clean(self):
        data = super().clean()
        if data.get("from_location") and data.get("from_location") == data.get("to_location"):
            raise forms.ValidationError("Choose two different locations.")
        return data


class StockAdjustForm(_StockForm):
    location = forms.ModelChoiceField(queryset=InventoryLocation.objects.all())
    counted_quantity = forms.DecimalField(**QTY_FIELD, label="Actual quantity counted")
    remark = forms.CharField(widget=TEXTAREA, label="Reason")


class StockWriteOffForm(_StockForm):
    location = forms.ModelChoiceField(queryset=InventoryLocation.objects.all())
    quantity = forms.DecimalField(**QTY_FIELD)
    reason = forms.CharField(widget=TEXTAREA, help_text="e.g. Expired, spoilt by rain, weevils.")


# =============================================================================
# Procurement
# =============================================================================
class PurchaseOrderForm(SchoolScopedFormMixin, forms.ModelForm):
    class Meta:
        model = PurchaseOrder
        fields = ["supplier", "order_date", "expected_date", "notes"]
        widgets = {"order_date": DATE, "expected_date": DATE, "notes": TEXTAREA}


class _LineForm(forms.ModelForm):
    def __init__(self, *args, school=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["item"].queryset = InventoryItem.objects.filter(school=school, is_active=True).order_by("name")
        for name, f in self.fields.items():
            if name != "DELETE":
                f.widget.attrs["class"] = "form-control form-control-sm"


class PurchaseOrderLineForm(_LineForm):
    class Meta:
        model = PurchaseOrderLine
        fields = ["item", "description", "quantity", "unit_cost"]


PurchaseOrderLineFormSet = inlineformset_factory(
    PurchaseOrder, PurchaseOrderLine, form=PurchaseOrderLineForm, extra=3, can_delete=True, min_num=1,
    validate_min=True)


class GoodsReceiptForm(SchoolScopedFormMixin, forms.ModelForm):
    class Meta:
        model = GoodsReceipt
        fields = ["supplier", "purchase_order", "received_date", "location", "delivery_note_no", "invoice_no",
                  "notes"]
        widgets = {"received_date": DATE, "notes": TEXTAREA}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["purchase_order"].queryset = PurchaseOrder.objects.filter(
            school=self.school, status__in=("APPROVED", "PARTIAL"))
        self.fields["purchase_order"].required = False

    def clean(self):
        data = super().clean()
        po = data.get("purchase_order")
        if po and data.get("supplier") and po.supplier_id != data["supplier"].pk:
            self.add_error("purchase_order", "This order belongs to a different supplier.")
        return data


class GoodsReceiptLineForm(_LineForm):
    class Meta:
        model = GoodsReceiptLine
        fields = ["item", "po_line", "quantity", "unit_cost"]

    def __init__(self, *args, purchase_order=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["po_line"].required = False
        self.fields["po_line"].queryset = (PurchaseOrderLine.objects.filter(purchase_order=purchase_order)
                                           if purchase_order else PurchaseOrderLine.objects.none())
        self.fields["po_line"].label = "Order line"

    def clean(self):
        data = super().clean()
        line, item = data.get("po_line"), data.get("item")
        if line and item and line.item_id != item.pk:
            self.add_error("po_line", "This order line is for a different item.")
        return data


GoodsReceiptLineFormSet = inlineformset_factory(
    GoodsReceipt, GoodsReceiptLine, form=GoodsReceiptLineForm, extra=3, can_delete=True, min_num=1,
    validate_min=True)


# =============================================================================
# Stock take
# =============================================================================
class StockTakeStartForm(SchoolScopedFormMixin, forms.Form):
    location = forms.ModelChoiceField(queryset=InventoryLocation.objects.all())
    notes = forms.CharField(required=False, widget=TEXTAREA)


class StockTakeCountForm(_StockForm):
    counted_quantity = forms.DecimalField(**QTY_FIELD)


class StockTakeCompleteForm(SchoolScopedFormMixin, forms.Form):
    apply_counts = forms.BooleanField(required=False, initial=True,
                                      label="Correct stock balances to the counted quantities")
    move_misplaced = forms.BooleanField(required=False, initial=True,
                                        label="Move tagged items found here (but recorded elsewhere) to this location")
    restore_found = forms.BooleanField(required=False, initial=True,
                                       label="Restore items marked lost that were found")
    mark_missing_lost = forms.BooleanField(required=False, initial=False,
                                           label="Mark items not found as lost")


# =============================================================================
# Reports
# =============================================================================
class ReportFilterForm(SchoolScopedFormMixin, forms.Form):
    date_from = forms.DateField(required=False, widget=DATE)
    date_to = forms.DateField(required=False, widget=DATE)
    location = forms.ModelChoiceField(queryset=InventoryLocation.objects.all(), required=False)
    class_ref = forms.ModelChoiceField(queryset=Class.objects.all(), required=False, label="Class")

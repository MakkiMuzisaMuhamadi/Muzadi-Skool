from django import forms
from mainApp.models.models import Class, Stream
from mainApp.models.subject_models import Subject, Paper  
from mainApp.models.subject_models import SubjectDraft, PaperDraft
from mainApp.models.teacher_models import TeacherTeachingAllocation 

BOOTSTRAP_INPUT = "form-control"
BOOTSTRAP_SELECT = "form-control"

class SubjectDraftStep1Form(forms.ModelForm):
    class Meta:
        model = SubjectDraft
        fields = [
            "code", "short_name", "name", "level",
            "is_major", "is_optional", "is_language", "is_Religion"
        ]
        widgets = {
            "code": forms.TextInput(attrs={"class": BOOTSTRAP_INPUT}),
            "short_name": forms.TextInput(attrs={"class": BOOTSTRAP_INPUT}),
            "name": forms.TextInput(attrs={"class": BOOTSTRAP_INPUT}),
            "level": forms.Select(attrs={"class": BOOTSTRAP_SELECT}),
        }

class PaperDraftRowForm(forms.ModelForm):
    class Meta:
        model = PaperDraft
        fields = ["paper_code", "paper_name", "max_marks", "order", "compulsory"]
        widgets = {
            "paper_code": forms.TextInput(attrs={"class": BOOTSTRAP_INPUT}),
            "paper_name": forms.TextInput(attrs={"class": BOOTSTRAP_INPUT}),
            "max_marks": forms.NumberInput(attrs={"class": BOOTSTRAP_INPUT}),
            "order": forms.NumberInput(attrs={"class": BOOTSTRAP_INPUT}),
            "compulsory": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }

class PaperEditForm(forms.ModelForm):
    class Meta:
        model = Paper
        fields = ["paper_code", "paper_name", "max_marks", "order", "compulsory"]
        widgets = {
            "paper_code": forms.TextInput(attrs={"class": BOOTSTRAP_INPUT}),
            "paper_name": forms.TextInput(attrs={"class": BOOTSTRAP_INPUT}),
            "max_marks": forms.NumberInput(attrs={"class": BOOTSTRAP_INPUT}),
            "order": forms.NumberInput(attrs={"class": BOOTSTRAP_INPUT}),
            "compulsory": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }
class SubjectEditForm(forms.ModelForm):
    class Meta:
        model = Subject
        fields = [
            "code", "short_name", "name", "level",
            "is_major", "is_optional", "is_language", "is_Religion",
        ]
        widgets = {
            "code": forms.TextInput(attrs={"class": BOOTSTRAP_INPUT}),
            "short_name": forms.TextInput(attrs={"class": BOOTSTRAP_INPUT}),
            "name": forms.TextInput(attrs={"class": BOOTSTRAP_INPUT}),
            "level": forms.Select(attrs={"class": BOOTSTRAP_SELECT}),
            "is_major": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "is_optional": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "is_language": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "is_Religion": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }


class PaperCreateForm(forms.ModelForm):
    class Meta:
        model = Paper
        fields = ["paper_code", "paper_name", "max_marks", "order", "compulsory"]
        widgets = {
            "paper_code": forms.TextInput(attrs={"class": BOOTSTRAP_INPUT}),
            "paper_name": forms.TextInput(attrs={"class": BOOTSTRAP_INPUT}),
            "max_marks": forms.NumberInput(attrs={"class": BOOTSTRAP_INPUT}),
            "order": forms.NumberInput(attrs={"class": BOOTSTRAP_INPUT}),
            "compulsory": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }


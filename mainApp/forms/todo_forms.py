from django import forms
from mainApp.models.todo_models import TodoTask

BOOTSTRAP_INPUT = "form-control"
BOOTSTRAP_SELECT = "form-control"
BOOTSTRAP_TEXTAREA = "form-control"


class TodoTaskForm(forms.ModelForm):
    class Meta:
        model = TodoTask
        fields = [
            "title", "description", "priority", "status",
            "due_date", "due_time", "is_important", "is_active"
        ]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 4}),
            "due_date": forms.DateInput(attrs={"type": "date"}),
            "due_time": forms.TimeInput(attrs={"type": "time"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        for _, field in self.fields.items():
            widget = field.widget
            if isinstance(widget, forms.Textarea):
                css = BOOTSTRAP_TEXTAREA
            elif isinstance(widget, (forms.Select, forms.DateInput, forms.TimeInput)):
                css = BOOTSTRAP_SELECT
            else:
                css = BOOTSTRAP_INPUT

            widget.attrs["class"] = f"{widget.attrs.get('class', '')} {css}".strip()
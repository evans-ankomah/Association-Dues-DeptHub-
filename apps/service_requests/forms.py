from django import forms

from .models import REQUEST_TYPES, ServiceRequest

INPUT = {"class": "field-input"}


class SubmitForm(forms.Form):
    request_type = forms.ChoiceField(
        label="What do you need help with?",
        choices=[("", "Choose…")] + [(t.code, t.label) for t in REQUEST_TYPES],
        widget=forms.Select(attrs=INPUT),
    )
    title = forms.CharField(max_length=180, label="Short summary", widget=forms.TextInput(attrs=INPUT))
    description = forms.CharField(
        label="Details",
        max_length=4000,
        widget=forms.Textarea(attrs={**INPUT, "rows": 6}),
        help_text="Include only what officers need to help you. "
        "Don't share passwords, PINs or medical details.",
    )
    priority = forms.ChoiceField(
        choices=[
            (ServiceRequest.Priority.LOW, "Low"),
            (ServiceRequest.Priority.NORMAL, "Normal"),
            (ServiceRequest.Priority.HIGH, "High: it's blocking my studies or work"),
        ],
        initial=ServiceRequest.Priority.NORMAL,
        widget=forms.Select(attrs=INPUT),
    )


class TriageForm(forms.Form):
    assignee = forms.ModelChoiceField(
        queryset=None, required=False, label="Assign to", widget=forms.Select(attrs=INPUT)
    )
    priority = forms.ChoiceField(choices=ServiceRequest.Priority.choices, widget=forms.Select(attrs=INPUT))

    def __init__(self, *args, assignable=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["assignee"].queryset = assignable
        self.fields["assignee"].label_from_instance = lambda u: u.display_name

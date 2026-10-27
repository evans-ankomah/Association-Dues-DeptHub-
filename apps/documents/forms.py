from django import forms

from .models import CATEGORIES, Document
from .services import OFFERED_VISIBILITY
from .storage import ALLOWED, max_upload_bytes

INPUT = {"class": "field-input"}
ACCEPT = ",".join(f".{ext}" for ext in ALLOWED)


class UploadForm(forms.Form):
    file = forms.FileField(widget=forms.ClearableFileInput(attrs={**INPUT, "accept": ACCEPT}))
    title = forms.CharField(max_length=180, widget=forms.TextInput(attrs=INPUT))
    category = forms.ChoiceField(choices=CATEGORIES, widget=forms.Select(attrs=INPUT))
    visibility = forms.ChoiceField(
        label="Who can see it",
        choices=[(v, label) for v, label in Document.Visibility.choices if v in OFFERED_VISIBILITY],
        initial=Document.Visibility.DEPARTMENT,
        widget=forms.Select(attrs={**INPUT, "x-model": "visibility"}),
        help_text="Financial evidence is only ever visible to finance officers, whatever you choose here.",
    )
    committee = forms.ModelChoiceField(queryset=None, required=False, widget=forms.Select(attrs=INPUT))

    def __init__(self, *args, committees=None, **kwargs):
        super().__init__(*args, **kwargs)
        limit = max_upload_bytes() // (1024 * 1024)
        help_text = f"PDF, Word, Excel, PowerPoint, CSV, text, PNG or JPEG, up to {limit} MB."
        self.fields["file"].help_text = help_text
        self.fields["committee"].queryset = committees
        if not committees.exists():
            self.fields["visibility"].choices = [
                c for c in self.fields["visibility"].choices if c[0] != Document.Visibility.COMMITTEE
            ]


class VersionForm(forms.Form):
    file = forms.FileField(widget=forms.ClearableFileInput(attrs={**INPUT, "accept": ACCEPT}))

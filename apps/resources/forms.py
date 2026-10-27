from django import forms

from apps.accounts.models import User

from .models import Asset

INPUT = {"class": "field-input"}
DATETIME = forms.DateTimeInput(attrs={**INPUT, "type": "datetime-local"}, format="%Y-%m-%dT%H:%M")


class AssetForm(forms.ModelForm):
    class Meta:
        model = Asset
        fields = [
            "asset_tag",
            "name",
            "asset_type",
            "description",
            "location",
            "condition_status",
            "availability_status",
            "custodian_user",
            "acquired_on",
            "replacement_cost",
        ]
        labels = {
            "asset_tag": "Inventory tag",
            "asset_type": "Type",
            "condition_status": "Condition",
            "availability_status": "Availability",
            "custodian_user": "Custodian",
            "replacement_cost": "Replacement cost (GHS)",
        }
        widgets = {
            "asset_tag": forms.TextInput(attrs=INPUT),
            "name": forms.TextInput(attrs=INPUT),
            "asset_type": forms.Select(attrs=INPUT),
            "description": forms.Textarea(attrs={**INPUT, "rows": 3}),
            "location": forms.TextInput(attrs=INPUT),
            "condition_status": forms.Select(attrs=INPUT),
            "availability_status": forms.Select(attrs=INPUT),
            "custodian_user": forms.Select(attrs=INPUT),
            "acquired_on": forms.DateInput(attrs={**INPUT, "type": "date"}, format="%Y-%m-%d"),
            "replacement_cost": forms.NumberInput(attrs={**INPUT, "step": "0.01", "min": "0"}),
        }

    def __init__(self, *args, custodians=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["custodian_user"].queryset = custodians if custodians is not None else User.objects.none()
        self.fields["custodian_user"].required = False
        self.fields["custodian_user"].label_from_instance = lambda u: u.display_name


class BookingForm(forms.Form):
    starts_at = forms.DateTimeField(label="From", widget=DATETIME)
    ends_at = forms.DateTimeField(label="Until", widget=DATETIME)
    purpose = forms.CharField(max_length=1000, widget=forms.Textarea(attrs={**INPUT, "rows": 3}))

    def clean(self):
        data = super().clean()
        if data.get("starts_at") and data.get("ends_at") and data["ends_at"] <= data["starts_at"]:
            self.add_error("ends_at", "Must be after the start.")
        return data


class ReturnForm(forms.Form):
    condition = forms.ChoiceField(
        choices=[("", "Unchanged")] + [c for c in Asset.Condition.choices if c[0] != Asset.Condition.RETIRED],
        required=False,
        label="Condition on return",
        widget=forms.Select(attrs=INPUT),
    )
    notes = forms.CharField(
        required=False, max_length=1000, widget=forms.Textarea(attrs={**INPUT, "rows": 2})
    )

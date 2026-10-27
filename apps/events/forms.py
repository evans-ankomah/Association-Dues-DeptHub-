from django import forms

from apps.finance.models import Budget

from .models import EVENT_TYPES, Event

INPUT = {"class": "field-input"}
DATETIME = {**INPUT, "type": "datetime-local"}
DT_FORMAT = "%Y-%m-%dT%H:%M"


class EventForm(forms.ModelForm):
    event_type = forms.ChoiceField(choices=EVENT_TYPES, widget=forms.Select(attrs=INPUT))

    class Meta:
        model = Event
        fields = [
            "title",
            "event_type",
            "description",
            "venue_text",
            "starts_at",
            "ends_at",
            "registration_required",
            "capacity",
            "related_budget",
        ]
        labels = {
            "venue_text": "Venue",
            "starts_at": "Starts",
            "ends_at": "Ends",
            "registration_required": "Members must register",
            "capacity": "Capacity (optional)",
            "related_budget": "Budget (optional)",
        }
        help_texts = {
            "capacity": "When places run out, further registrations join a waitlist.",
            "related_budget": "Only approved budgets for this department are listed.",
        }
        widgets = {
            "title": forms.TextInput(attrs=INPUT),
            "description": forms.Textarea(attrs={**INPUT, "rows": 5}),
            "venue_text": forms.TextInput(attrs=INPUT),
            "starts_at": forms.DateTimeInput(attrs=DATETIME, format=DT_FORMAT),
            "ends_at": forms.DateTimeInput(attrs=DATETIME, format=DT_FORMAT),
            "capacity": forms.NumberInput(attrs={**INPUT, "min": "1"}),
            "related_budget": forms.Select(attrs=INPUT),
        }

    def __init__(self, *args, department=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["related_budget"].queryset = Budget.objects.filter(
            department=department, budget_status=Budget.Status.APPROVED
        )
        self.fields["related_budget"].required = False
        if self.instance.event_type and self.instance.event_type not in dict(EVENT_TYPES):
            self.fields["event_type"].choices = [
                *EVENT_TYPES,
                (self.instance.event_type, self.instance.event_type.replace("_", " ").capitalize()),
            ]

    def clean(self):
        data = super().clean()
        starts, ends = data.get("starts_at"), data.get("ends_at")
        if starts and ends and ends <= starts:
            self.add_error("ends_at", "Must be after the start.")
        if data.get("capacity") is not None and data["capacity"] < 1:
            self.add_error("capacity", "Enter at least 1, or leave it empty for no limit.")
        return data

from django import forms

from apps.events.models import Event
from apps.memberships.models import DepartmentMembership

from .models import Committee, Meeting, Task

INPUT = {"class": "field-input"}
DATETIME = forms.DateTimeInput(attrs={**INPUT, "type": "datetime-local"}, format="%Y-%m-%dT%H:%M")


def _member_label(m):
    profile = getattr(m.user, "profile", None)
    name = profile.full_name if profile else m.user.email
    return f"{name} · {m.level_code}" if m.level_code else name


class CommitteeForm(forms.ModelForm):
    class Meta:
        model = Committee
        fields = ["name", "description", "chair_membership", "handover_notes"]
        labels = {"chair_membership": "Chair (optional)", "handover_notes": "Handover notes"}
        help_texts = {
            "handover_notes": "What next session's committee should know: contacts, open issues, lessons."
        }
        widgets = {
            "name": forms.TextInput(attrs=INPUT),
            "description": forms.Textarea(attrs={**INPUT, "rows": 3}),
            "chair_membership": forms.Select(attrs=INPUT),
            "handover_notes": forms.Textarea(attrs={**INPUT, "rows": 4}),
        }

    def __init__(self, *args, members=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["chair_membership"].queryset = members
        self.fields["chair_membership"].required = False
        self.fields["chair_membership"].label_from_instance = _member_label


class AddMemberForm(forms.Form):
    membership = forms.ModelChoiceField(
        queryset=DepartmentMembership.objects.none(), label="Member", widget=forms.Select(attrs=INPUT)
    )
    role_title = forms.CharField(
        max_length=80, initial="Member", label="Role on the committee", widget=forms.TextInput(attrs=INPUT)
    )

    def __init__(self, *args, members=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["membership"].queryset = members
        self.fields["membership"].label_from_instance = _member_label


class MeetingForm(forms.ModelForm):
    class Meta:
        model = Meeting
        fields = ["title", "committee", "scheduled_at", "location_text", "notes", "minutes_document"]
        labels = {
            "committee": "Committee",
            "scheduled_at": "When",
            "location_text": "Where",
            "notes": "Agenda or minutes",
            "minutes_document": "Minutes document (optional)",
        }
        help_texts = {
            "committee": "Leave empty for an executive meeting (visible to department executives only).",
            "location_text": "A room or a meeting-link label. Don't paste passwords or access codes.",
        }
        widgets = {
            "title": forms.TextInput(attrs=INPUT),
            "committee": forms.Select(attrs=INPUT),
            "scheduled_at": DATETIME,
            "location_text": forms.TextInput(attrs=INPUT),
            "notes": forms.Textarea(attrs={**INPUT, "rows": 8}),
            "minutes_document": forms.Select(attrs=INPUT),
        }

    def __init__(self, *args, committees=None, allow_executive=True, minutes_documents=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["committee"].queryset = committees
        self.fields["committee"].required = not allow_executive
        self.fields["minutes_document"].queryset = minutes_documents
        self.fields["minutes_document"].required = False
        self.fields["minutes_document"].help_text = "Upload the signed minutes under Documents first."


class TaskForm(forms.ModelForm):
    class Meta:
        model = Task
        fields = [
            "title",
            "description",
            "assigned_membership",
            "priority",
            "due_at",
            "committee",
            "event",
            "meeting",
        ]
        labels = {
            "assigned_membership": "Assign to",
            "due_at": "Due",
            "event": "Event",
            "meeting": "From meeting",
        }
        widgets = {
            "title": forms.TextInput(attrs=INPUT),
            "description": forms.Textarea(attrs={**INPUT, "rows": 4}),
            "assigned_membership": forms.Select(attrs=INPUT),
            "priority": forms.Select(attrs=INPUT),
            "due_at": DATETIME,
            "committee": forms.Select(attrs=INPUT),
            "event": forms.Select(attrs=INPUT),
            "meeting": forms.Select(attrs=INPUT),
        }

    def __init__(self, *args, members=None, committees=None, events=None, meetings=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["assigned_membership"].queryset = members
        self.fields["assigned_membership"].label_from_instance = _member_label
        self.fields["committee"].queryset = committees
        self.fields["event"].queryset = events if events is not None else Event.objects.none()
        self.fields["meeting"].queryset = meetings
        for name in ("assigned_membership", "committee", "event", "meeting"):
            self.fields[name].required = False

"""The generated sample events load 1:1 and agree with the attendance summary definitions."""

from pathlib import Path

import pytest

from apps.common import seeding
from apps.events import services as svc
from apps.events.models import Event, EventAttendance, EventRegistration

SEED_DIR = Path(__file__).resolve().parents[3] / "seed" / "output" / "json"

pytestmark = [
    pytest.mark.django_db,
    pytest.mark.skipif(not (SEED_DIR / "users.json").exists(), reason="run python seed/generate_umat.py"),
]


def test_seed_events_load_and_summarise():
    seeding.load(SEED_DIR, "pw")
    assert Event.objects.count() > 0
    # Seed data respects the constraints the services enforce.
    for event in Event.objects.exclude(capacity=None):
        assert svc.places_taken(event) <= event.capacity, event.title
    regs_by_event = {}
    for r in EventRegistration.objects.all():
        regs_by_event.setdefault(r.event_id, set()).add(r.department_membership_id)
    for a in EventAttendance.objects.filter(event__registration_required=True):
        assert a.department_membership_id in regs_by_event.get(a.event_id, set())

    rows = {r["event"].title: r for r in svc.attendance_summary(Event.objects.all())}
    completed_with_reg = [
        r for r in rows.values() if r["event"].registration_required and r["event"].registered
    ]
    assert completed_with_reg and all(0 <= (r["rate"] or 0) <= 100 for r in completed_with_reg)

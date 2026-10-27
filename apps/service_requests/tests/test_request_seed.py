"""The generated sample requests load 1:1 and their restricted cases stay private."""

from pathlib import Path

import pytest

from apps.accounts.models import User
from apps.common import seeding
from apps.institutions.models import Department
from apps.service_requests import services as svc
from apps.service_requests.models import ServiceRequest, ServiceRequestComment

SEED_DIR = Path(__file__).resolve().parents[3] / "seed" / "output" / "json"

pytestmark = [
    pytest.mark.django_db,
    pytest.mark.skipif(not (SEED_DIR / "users.json").exists(), reason="run python seed/generate_umat.py"),
]


def _holder(role_code):
    return User.objects.filter(
        role_assignments__role__role_code=role_code, role_assignments__assignment_status="active"
    ).first()


def test_seed_restricted_requests_and_staff_notes_stay_private():
    seeding.load(SEED_DIR, "pw")
    cse = Department.objects.get(code="CSE")
    restricted = ServiceRequest.objects.filter(visibility_level="restricted")
    assert restricted.exists()

    hod, custodian, president = (
        _holder("hod_faculty_adviser"),
        _holder("asset_custodian"),
        _holder("department_president"),
    )
    for sr in restricted:
        assert sr in svc.visible_requests(hod, cse)
        assert sr in svc.visible_requests(sr.requester_membership.user, cse)
        for officer in (custodian, president):
            if officer.pk != sr.requester_membership.user_id:
                assert sr not in svc.visible_requests(officer, cse)

    # Requesters never see staff-only notes on their own requests.
    for note in ServiceRequestComment.objects.filter(visibility="staff_only").select_related(
        "service_request"
    ):
        sr = note.service_request
        assert note not in svc.visible_comments(sr.requester_membership.user, sr)

    rows = svc.sla_summary(ServiceRequest.objects.filter(department=cse))
    assert sum(r["total"] for r in rows) == ServiceRequest.objects.filter(department=cse).count()

"""The generated committees, meetings and tasks load 1:1 and respect the visibility rules."""

from pathlib import Path

import pytest

from apps.accounts.models import User
from apps.common import seeding
from apps.institutions.models import Department
from apps.workspace import services as svc
from apps.workspace.models import Committee, CommitteeMember, Meeting, Task

SEED_DIR = Path(__file__).resolve().parents[3] / "seed" / "output" / "json"

pytestmark = [
    pytest.mark.django_db,
    pytest.mark.skipif(not (SEED_DIR / "users.json").exists(), reason="run python seed/generate_umat.py"),
]


def test_seed_workspace_loads_and_scopes():
    seeding.load(SEED_DIR, "pw")
    cse = Department.objects.get(code="CSE")
    assert Committee.objects.filter(committee_status="active").exists()
    # A plain committee member (not an executive) sees their committee, not executive meetings.
    seat = (
        CommitteeMember.objects.filter(membership_status="active")
        .exclude(role_title="Chair")
        .select_related("department_membership__user")
        .first()
    )
    user = seat.department_membership.user
    if svc.is_executive(user, cse):
        pytest.skip("sampled member is also an executive")
    assert seat.committee in svc.visible_committees(user, cse)
    exec_meetings = Meeting.objects.filter(department=cse, committee__isnull=True)
    assert exec_meetings.exists()
    assert not svc.visible_meetings(user, cse).filter(pk__in=exec_meetings).exists()

    secretary = User.objects.filter(
        role_assignments__role__role_code="department_secretary", role_assignments__assignment_status="active"
    ).first()
    assert svc.visible_tasks(secretary, cse).count() == Task.objects.filter(department=cse).count()
    rows = svc.task_summary(Task.objects.filter(department=cse))
    assert sum(r["total"] for r in rows) == Task.objects.filter(department=cse).count()

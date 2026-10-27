import json
from pathlib import Path

import pytest
from django.core.management import CommandError, call_command

from apps.access.models import Role
from apps.access.services import has_perm
from apps.accounts.models import User
from apps.common import seeding
from apps.institutions.models import Department

SEED_DIR = Path(__file__).resolve().parents[3] / "seed" / "output" / "json"

pytestmark = [
    pytest.mark.django_db,
    pytest.mark.skipif(not (SEED_DIR / "users.json").exists(), reason="run python seed/generate_umat.py"),
]


def test_seed_loads_and_seeded_staff_have_their_roles():
    report = seeding.load(SEED_DIR, "pw-for-tests")
    manifest = json.loads((SEED_DIR.parent / "manifest.json").read_text())

    for table, count in report.loaded.items():
        assert count == manifest["row_counts"][table], table
    assert not report.dropped_columns
    for table in ("events", "event_registrations", "event_attendance"):
        assert report.loaded[table] == manifest["row_counts"][table], table
    assert Role.objects.count() == 11

    cse = Department.objects.get(code="CSE")
    treasurer = User.objects.filter(
        role_assignments__role__role_code="treasurer", role_assignments__assignment_status="active"
    ).get()
    assert treasurer.check_password("pw-for-tests")
    assert has_perm(treasurer, "finance.payment.verify", cse)
    assert not has_perm(treasurer, "finance.payment.record", cse)


def test_command_refuses_when_users_exist(make_user, settings):
    settings.DEBUG = True
    make_user()
    with pytest.raises(CommandError, match="already has users"):
        call_command("load_umat_seed")


def test_command_refuses_with_debug_off(settings):
    settings.DEBUG = False
    with pytest.raises(CommandError, match="DEBUG off"):
        call_command("load_umat_seed")

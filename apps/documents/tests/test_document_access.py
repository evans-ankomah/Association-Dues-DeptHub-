"""Permission-leak tests: restricted documents must stay private, through every path."""

import pytest

from apps.common.tests.factories import DepartmentFactory, grant
from apps.documents import services as svc
from apps.documents.models import Document
from apps.documents.tests.conftest import pdf
from apps.workspace import services as ws

pytestmark = pytest.mark.django_db


def _can(client, doc):
    """Whether the user can reach a document by detail page, download or list."""
    detail = client.get(f"/documents/{doc.pk}/").status_code
    download = client.get(f"/documents/{doc.pk}/download/").status_code
    listed = doc in client.get("/documents/?status=").context["page"]
    assert (detail == 200) == (download == 200) == listed, (detail, download, listed)
    if detail != 200:
        assert detail == download == 404
    return detail == 200


def test_member_sees_approved_department_documents_only(approved, upload, secretary, make_member, login):
    shared = approved()
    draft = upload(secretary, title="Draft budget notes")
    pending = upload(secretary, title="Pending", submit_for_review=True)
    client = login(make_member())
    assert _can(client, shared)
    assert not _can(client, draft)
    assert not _can(client, pending)


def test_owner_only_documents_are_private_even_from_stewards(
    upload, secretary, president, make_member, treasurer, login
):
    private = upload(secretary, visibility="owner_only", title="My notes")
    assert _can(login(secretary), private)
    for other in (president, treasurer, make_member()):
        assert not _can(login(other), private)


def test_financial_evidence_only_for_finance_officers(
    upload, treasurer, secretary, president, make_member, make_user, login
):
    invoice = upload(treasurer, category="financial", title="Vendor invoice", submit_for_review=True)
    svc.review(invoice, actor=president, approve=True)
    hod = make_user("hod_faculty_adviser")
    auditor = make_user("auditor")
    finsec = make_member("financial_secretary")
    # The President approves expenses (step 1), so they see the evidence too.
    for user in (treasurer, hod, auditor, finsec, president):
        assert _can(login(user), invoice), user
    # The secretary stewards documents but has no finance role, and members never see financial
    # evidence even when it's approved and "department" visible.
    for user in (secretary, make_member(), make_member("executive_officer")):
        assert not _can(login(user), invoice), user


def test_committee_documents_only_for_that_committee(approved, committee, secretary, make_member, login):
    doc = approved(visibility="committee", committee=committee)
    seated, outsider = make_member(), make_member()
    ws.add_member(committee, seated.memberships.get(), actor=secretary)
    assert _can(login(seated), doc)
    assert not _can(login(outsider), doc)
    # Leaving the committee removes access.
    ws.end_membership(committee.members.get(department_membership__user=seated), actor=secretary)
    assert not _can(login(seated), doc)


def test_other_departments_documents_are_invisible(department, secretary, login, make_user):
    other = DepartmentFactory(institution=department.institution)
    foreign_secretary = make_user()
    grant(foreign_secretary, "department_secretary", other)
    foreign = svc.upload(
        other,
        actor=foreign_secretary,
        file=pdf(),
        title="Foreign",
        category="policy",
        visibility="department",
    )
    foreign.review_status = Document.Review.APPROVED
    foreign.save()
    client = login(secretary)
    assert client.get(f"/documents/{foreign.pk}/").status_code == 404
    assert client.get(f"/documents/{foreign.pk}/download/").status_code == 404
    assert client.post(f"/documents/{foreign.pk}/action/", {"action": "archive"}).status_code == 404
    assert client.post(f"/documents/{foreign.pk}/version/", {"file": pdf()}).status_code == 404


def test_member_cannot_upload_review_or_archive_through_views(
    approved, upload, secretary, make_member, login
):
    member = make_member()
    client = login(member)
    assert client.get("/documents/upload/").status_code == 403
    assert (
        client.post(
            "/documents/upload/",
            {"file": pdf(), "title": "x", "category": "policy", "visibility": "department"},
        ).status_code
        == 403
    )
    pending = upload(secretary, submit_for_review=True)
    shared = approved(title="Shared")
    client.post(f"/documents/{shared.pk}/action/", {"action": "archive"})
    shared.refresh_from_db()
    assert shared.review_status == "approved"
    assert client.post(f"/documents/{pending.pk}/action/", {"action": "approve"}).status_code == 404
    pending.refresh_from_db()
    assert pending.review_status == "pending_review"


def test_older_restricted_version_does_not_leak_through_version_list(
    approved, upload, secretary, president, make_member, login
):
    v1 = approved()
    v2 = svc.add_version(v1, actor=secretary, file=pdf())  # draft, not yet approved
    member_client = login(make_member())
    assert not _can(member_client, v2)
    page = member_client.get(f"/documents/{v1.pk}/")
    assert v2 not in page.context["versions"]


def test_download_headers_force_attachment(approved, make_member, login):
    doc = approved()
    response = login(make_member()).get(f"/documents/{doc.pk}/download/")
    assert response["Content-Disposition"].startswith("attachment;")
    assert response["X-Content-Type-Options"] == "nosniff"
    assert "no-store" in response["Cache-Control"]


def test_upload_through_view(secretary, login):
    client = login(secretary)
    response = client.post(
        "/documents/upload/",
        {
            "file": pdf(),
            "title": "Minutes 12 Oct",
            "category": "minutes",
            "visibility": "department",
            "submit": "1",
        },
    )
    doc = Document.objects.get()
    assert response.status_code == 302 and doc.review_status == "pending_review"


def test_disguised_upload_through_view_shows_error(secretary, login):
    from django.core.files.uploadedfile import SimpleUploadedFile

    response = login(secretary).post(
        "/documents/upload/",
        {
            "file": SimpleUploadedFile("x.pdf", b"<script>"),
            "title": "x",
            "category": "policy",
            "visibility": "department",
        },
    )
    assert response.status_code == 200 and not Document.objects.exists()
    assert "match its type" in response.content.decode()


def test_meeting_minutes_link_respects_document_visibility(upload, secretary, committee, make_member, login):
    import datetime

    from django.utils import timezone

    from apps.workspace.models import Meeting

    draft_minutes = upload(
        secretary, category="minutes", title="DRAFT-MINUTES", visibility="committee", committee=committee
    )
    meeting = ws.save_meeting(
        Meeting(
            department=secretary.memberships.get().department,
            committee=committee,
            title="Welfare cases",
            scheduled_at=timezone.now() - datetime.timedelta(days=1),
            minutes_document=draft_minutes,
        ),
        actor=secretary,
    )
    seated = make_member()
    ws.add_member(committee, seated.memberships.get(), actor=secretary)
    page = login(seated).get(f"/workspace/meetings/{meeting.pk}/")
    assert page.status_code == 200 and b"DRAFT-MINUTES" not in page.content  # not approved yet
    assert b"DRAFT-MINUTES" in login(secretary).get(f"/workspace/meetings/{meeting.pk}/").content


def test_cannot_link_minutes_the_actor_cannot_see(upload, secretary, committee, make_member):
    import datetime

    from django.utils import timezone

    from apps.workspace.models import Meeting

    private = upload(secretary, category="minutes", visibility="owner_only")
    chair = make_member()
    committee.chair_membership = chair.memberships.get()
    ws.save_committee(committee, actor=secretary)
    meeting = Meeting(
        department=committee.department,
        committee=committee,
        title="x",
        scheduled_at=timezone.now() + datetime.timedelta(days=1),
        minutes_document=private,
    )
    with pytest.raises(ws.WorkspaceError, match="minutes document"):
        ws.save_meeting(meeting, actor=chair)

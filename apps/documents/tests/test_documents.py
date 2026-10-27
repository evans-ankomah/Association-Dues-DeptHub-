import hashlib

import pytest
from django.core.exceptions import PermissionDenied
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.audit.models import AuditEvent
from apps.documents import services as svc
from apps.documents import storage
from apps.documents.models import Document
from apps.documents.tests.conftest import PDF, docx, pdf

pytestmark = pytest.mark.django_db


# ----- upload and storage -------------------------------------------------------------


def test_upload_records_metadata_and_stores_privately(upload, secretary):
    doc = upload(secretary)
    assert doc.checksum_sha256 == hashlib.sha256(PDF).hexdigest()
    assert doc.file_size_bytes == len(PDF) and doc.mime_type == "application/pdf"
    assert doc.review_status == "draft" and doc.version_number == 1
    assert doc.storage_key.startswith(f"{doc.department_id}/") and "minutes" not in doc.storage_key
    assert storage.storage().exists(doc.storage_key)


def test_real_document_storage_is_private_and_outside_media():
    """Outside tests, documents go to a folder that's never served and has no public URL."""
    from config.settings import base

    configured = base.STORAGES["documents"]
    assert configured["BACKEND"] == "django.core.files.storage.FileSystemStorage"
    assert configured["OPTIONS"]["base_url"] is None
    root = base.PRIVATE_MEDIA_ROOT.resolve()
    for public in (base.MEDIA_ROOT, base.STATIC_ROOT, *base.STATICFILES_DIRS):
        public = public.resolve()
        assert root != public and public not in root.parents and root not in public.parents


@pytest.mark.parametrize(
    "file, message",
    [
        (SimpleUploadedFile("run.exe", b"MZ\x90\x00"), "isn't allowed"),
        (SimpleUploadedFile("fake.pdf", b"<html><script>alert(1)</script>"), "don't match"),
        (SimpleUploadedFile("empty.pdf", b""), "empty"),
        (SimpleUploadedFile("macro.docx", b"PK\x03\x04garbage"), "don't match"),
        (SimpleUploadedFile("page.html", b"<html></html>"), "isn't allowed"),
        (SimpleUploadedFile("photo.png", PDF), "don't match"),
    ],
)
def test_rejects_disallowed_or_disguised_files(upload, secretary, file, message):
    with pytest.raises(svc.DocumentError, match=message):
        upload(secretary, file=file)
    assert not Document.objects.exists()


def test_rejects_oversized_files(upload, secretary, settings):
    settings.DOCUMENT_MAX_UPLOAD_BYTES = 10
    with pytest.raises(svc.DocumentError, match="at most"):
        upload(secretary)


def test_accepts_office_files(upload, secretary):
    assert upload(secretary, file=docx()).mime_type.endswith("wordprocessingml.document")


def test_filename_is_sanitised(upload, secretary):
    doc = upload(secretary, file=pdf(name='..\\..\\evil"<x>.pdf'))
    assert doc.original_filename == "evilx.pdf"


def test_members_cannot_upload(upload, make_member):
    with pytest.raises(PermissionDenied):
        upload(make_member())


def test_only_finance_files_financial_evidence(upload, secretary, treasurer):
    with pytest.raises(svc.DocumentError, match="finance officers"):
        upload(secretary, category="financial")
    assert upload(treasurer, category="financial", title="Invoice 42").category == "financial"


def test_committee_documents_need_a_committee_the_uploader_manages(upload, secretary, committee, make_member):
    with pytest.raises(svc.DocumentError, match="Choose the committee"):
        upload(secretary, visibility="committee")
    assert upload(secretary, visibility="committee", committee=committee).committee == committee


def test_chair_without_officer_role_uploads_for_own_committee_only(
    upload, department, session, committee, secretary, make_member
):
    from apps.workspace import services as ws

    chair = make_member()
    committee.chair_membership = chair.memberships.get()
    ws.save_committee(committee, actor=secretary)
    assert upload(chair, visibility="committee", committee=committee).uploaded_by == chair
    with pytest.raises(svc.DocumentError, match="committees you chair"):
        upload(chair, visibility="department")


def test_unoffered_visibility_rejected(upload, secretary):
    with pytest.raises(svc.DocumentError, match="who can see"):
        upload(secretary, visibility="public")


# ----- review and versions ------------------------------------------------------------


def test_review_needs_a_different_steward(upload, secretary, president):
    doc = upload(secretary, submit_for_review=True)
    with pytest.raises(svc.DocumentError, match="other than the uploader"):
        svc.review(doc, actor=secretary, approve=True)
    svc.review(doc, actor=president, approve=True)
    doc.refresh_from_db()
    assert doc.review_status == "approved"


def test_member_cannot_review(upload, secretary, make_member):
    doc = upload(secretary, submit_for_review=True)
    with pytest.raises(PermissionDenied):
        svc.review(doc, actor=make_member(), approve=True)


def test_new_version_supersedes_old_once_approved(approved, secretary, president):
    v1 = approved()
    v2 = svc.add_version(v1, actor=secretary, file=pdf(body=PDF + b"%v2"), submit_for_review=True)
    assert v2.version_number == 2 and v2.previous_version_document == v1
    v1.refresh_from_db()
    assert v1.review_status == "approved"  # still the live version until v2 is approved
    svc.review(v2, actor=president, approve=True)
    v1.refresh_from_db()
    assert v1.review_status == "archived"
    assert [d.version_number for d in svc.versions(v1, secretary)] == [2, 1]
    with pytest.raises(svc.DocumentError, match="can't add a version"):
        svc.add_version(v1, actor=secretary, file=pdf())  # only the latest takes new versions


def test_member_cannot_add_version(approved, make_member):
    with pytest.raises(svc.DocumentError, match="can't add a version"):
        svc.add_version(approved(), actor=make_member(), file=pdf())


def test_private_documents_are_not_reviewed(upload, secretary):
    doc = upload(secretary, visibility="owner_only")
    assert doc.review_status == "approved"
    with pytest.raises(svc.DocumentError, match="don't need review"):
        svc.submit_for_review(doc, actor=secretary)


def test_download_is_audited(approved, make_member):
    doc = approved()
    member = make_member()
    with svc.open_for_download(doc, actor=member) as handle:
        assert handle.read() == PDF
    event = AuditEvent.objects.get(action_key="document.downloaded")
    assert event.actor_user == member and event.entity_id == doc.pk


def test_audit_never_contains_storage_key(upload, secretary):
    doc = upload(secretary)
    dumped = str(list(AuditEvent.objects.filter(entity_id=doc.pk).values("after_data")))
    assert doc.storage_key not in dumped

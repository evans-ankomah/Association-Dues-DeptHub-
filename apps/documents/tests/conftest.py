import io
import zipfile

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.common.tests.factories import MembershipFactory
from apps.documents import services as svc
from apps.workspace import services as ws
from apps.workspace.models import Committee

PDF = b"%PDF-1.7\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


def pdf(name="minutes.pdf", body=PDF):
    return SimpleUploadedFile(name, body, content_type="application/pdf")


def docx(name="policy.docx"):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", "<w:document/>")
    return SimpleUploadedFile(name, buffer.getvalue())


@pytest.fixture
def make_member(make_user, department, session):
    def _make(role="student_member", **kwargs):
        user = make_user(role)
        MembershipFactory(user=user, department=department, academic_session=session, **kwargs)
        return user

    return _make


@pytest.fixture
def secretary(make_member):
    return make_member("department_secretary")


@pytest.fixture
def president(make_member):
    return make_member("department_president")


@pytest.fixture
def treasurer(make_member):
    return make_member("treasurer")


@pytest.fixture
def committee(department, session, secretary):
    return ws.save_committee(
        Committee(department=department, academic_session=session, name="Welfare"), actor=secretary
    )


@pytest.fixture
def upload(department):
    def _upload(
        actor, *, file=None, title="Constitution", category="policy", visibility="department", **kwargs
    ):
        return svc.upload(
            department,
            actor=actor,
            file=file or pdf(),
            title=title,
            category=category,
            visibility=visibility,
            **kwargs,
        )

    return _upload


@pytest.fixture
def approved(upload, secretary, president):
    def _approved(**kwargs):
        doc = upload(secretary, submit_for_review=True, **kwargs)
        return svc.review(doc, actor=president, approve=True)

    return _approved

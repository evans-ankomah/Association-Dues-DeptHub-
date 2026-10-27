"""Permission-leak tests for service requests through the web pages and the API."""

import pytest

from apps.common.tests.factories import DepartmentFactory, MembershipFactory, grant
from apps.service_requests import services as svc
from apps.service_requests.models import ServiceRequest, ServiceRequestComment

pytestmark = pytest.mark.django_db


def test_member_cannot_open_another_members_request(requester, make_member, submit, login):
    sr = submit(requester)
    client = login(make_member())
    assert client.get(f"/requests/{sr.pk}/").status_code == 404
    assert client.post(f"/requests/{sr.pk}/comment/", {"body": "hi"}).status_code == 404
    assert client.post(f"/requests/{sr.pk}/requester/", {"action": "withdraw"}).status_code == 404
    assert client.get(f"/api/v1/requests/{sr.pk}/").status_code == 404
    sr.refresh_from_db()
    assert sr.request_status == "submitted" and not sr.comments.exists()


def test_member_list_shows_only_own_requests(requester, make_member, submit, login):
    mine = submit(requester)
    submit(make_member(), title="Someone else's")
    client = login(requester)
    assert list(client.get("/requests/").context["page"]) == [mine]
    assert [r["id"] for r in client.get("/api/v1/requests/").json()["results"]] == [str(mine.pk)]


def test_member_cannot_reach_queue_or_handler_actions(requester, make_member, submit, login):
    sr = submit(make_member())
    client = login(requester)
    assert client.get("/requests/queue/").status_code == 403
    assert client.get("/api/v1/requests/?scope=queue").status_code == 403
    own = submit(requester)
    for target in (own, sr):
        response = client.post(
            f"/requests/{target.pk}/handle/", {"action": "resolve", "summary": "Done by me"}
        )
        assert response.status_code == 403
        target.refresh_from_db()
        assert target.request_status == "submitted"


def test_staff_only_comment_never_reaches_requester_page_or_api(requester, custodian, submit, login):
    sr = submit(requester)
    svc.add_comment(sr, actor=custodian, body="Visible reply")
    svc.add_comment(sr, actor=custodian, body="INTERNAL-JOB-CARD", staff_only=True)
    client = login(requester)
    page = client.get(f"/requests/{sr.pk}/")
    assert b"Visible reply" in page.content and b"INTERNAL-JOB-CARD" not in page.content
    api = client.get(f"/api/v1/requests/{sr.pk}/").json()
    assert [c["body"] for c in api["comments"]] == ["Visible reply"]
    officer = login(custodian).get(f"/requests/{sr.pk}/")
    assert b"INTERNAL-JOB-CARD" in officer.content


def test_requester_posting_staff_only_flag_is_rejected(requester, submit, login):
    sr = submit(requester)
    login(requester).post(f"/requests/{sr.pk}/comment/", {"body": "sneaky", "staff_only": "1"})
    assert not ServiceRequestComment.objects.filter(visibility="staff_only").exists()


def test_restricted_request_is_invisible_to_other_officers(
    requester, hod, custodian, president, submit, login
):
    sr = submit(requester, request_type="result_query", title="RESTRICTED-TITLE")
    for officer in (custodian, president):
        client = login(officer)
        assert client.get(f"/requests/{sr.pk}/").status_code == 404
        assert client.post(f"/requests/{sr.pk}/handle/", {"action": "take"}).status_code == 404
        assert client.post(f"/requests/{sr.pk}/comment/", {"body": "x", "staff_only": "1"}).status_code == 404
        queue = client.get("/requests/queue/?status=")
        assert sr not in queue.context["page"] and b"RESTRICTED-TITLE" not in queue.content
        api = client.get("/api/v1/requests/?scope=queue").json()
        assert str(sr.pk) not in [r["id"] for r in api["results"]]
        assert client.get(f"/api/v1/requests/{sr.pk}/").status_code == 404
    assert login(hod).get(f"/requests/{sr.pk}/").status_code == 200
    assert login(requester).get(f"/requests/{sr.pk}/").status_code == 200


def test_auditor_and_treasurer_cannot_see_requests(requester, make_member, make_user, submit, login):
    sr = submit(requester)
    for user in (make_user("auditor"), make_member("treasurer")):
        client = login(user)
        assert client.get("/requests/queue/").status_code == 403
        assert client.get(f"/requests/{sr.pk}/").status_code == 404


def test_other_departments_requests_are_invisible(requester, custodian, hod, department, submit, login):
    other = DepartmentFactory(institution=department.institution)
    outsider = MembershipFactory(
        department=other, academic_session=requester.memberships.get().academic_session
    )
    grant(outsider.user, "student_member", other)
    foreign = svc.submit(
        other, actor=outsider.user, request_type="introductory_letter", title="FOREIGN", description="x"
    )
    for user in (custodian, hod):
        client = login(user)
        assert client.get(f"/requests/{foreign.pk}/").status_code == 404
        assert client.post(f"/requests/{foreign.pk}/handle/", {"action": "take"}).status_code == 404
        assert b"FOREIGN" not in client.get("/requests/queue/?status=").content


def test_submit_and_handle_through_views(requester, custodian, login):
    client = login(requester)
    client.post(
        "/requests/new/",
        {
            "request_type": "equipment_fault",
            "title": "Projector broken",
            "description": "No HDMI",
            "priority": "high",
        },
    )
    sr = ServiceRequest.objects.get()
    assert sr.priority == "high"
    officer = login(custodian)
    assert sr in officer.get("/requests/queue/").context["page"]
    officer.post(f"/requests/{sr.pk}/handle/", {"action": "take"})
    officer.post(f"/requests/{sr.pk}/handle/", {"action": "resolve", "summary": "Fixed"})
    sr.refresh_from_db()
    assert sr.request_status == "resolved" and sr.assigned_to_user == custodian


def test_officer_does_not_see_own_request_in_queue(president, custodian, submit, login):
    sr = submit(president)
    assert sr not in login(president).get("/requests/queue/").context["page"]
    page = login(president).get(f"/requests/{sr.pk}/")
    assert page.status_code == 200 and not page.context["is_handler"]


def test_api_submit(requester, login):
    response = login(requester).post(
        "/api/v1/requests/",
        {"request_type": "lab_access", "title": "Lab 2 evenings", "description": "Project work"},
        content_type="application/json",
    )
    assert response.status_code == 201, response.content
    assert response.json()["reference"].startswith("SR-")


def test_api_restricted_without_handler_is_400(requester, login):
    response = login(requester).post(
        "/api/v1/requests/",
        {"request_type": "result_query", "title": "x", "description": "y"},
        content_type="application/json",
    )
    assert response.status_code == 400

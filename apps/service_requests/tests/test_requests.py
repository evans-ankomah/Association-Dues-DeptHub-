import datetime

import pytest
from django.core.exceptions import PermissionDenied
from django.utils import timezone

from apps.audit.models import AuditEvent
from apps.communications.models import Notification
from apps.service_requests import services as svc
from apps.service_requests.models import ServiceRequest, ServiceRequestComment

pytestmark = pytest.mark.django_db


def test_submit_sets_reference_due_date_and_department_visibility(requester, submit):
    sr = submit(requester)
    assert sr.request_status == "submitted" and sr.visibility_level == "department"
    assert sr.reference.startswith("SR-") and len(sr.reference) == 11
    assert sr.due_at - sr.submitted_at == datetime.timedelta(days=6)
    assert sr.assigned_to_user is None


def test_members_cannot_self_set_urgent(requester, submit):
    assert submit(requester, priority="urgent").priority == "normal"


def test_unknown_type_rejected(requester, submit):
    with pytest.raises(svc.RequestError, match="Choose a request type"):
        submit(requester, request_type="welfare")


def test_needs_requests_create_and_a_membership(custodian, make_user, submit):
    with pytest.raises(PermissionDenied):
        submit(custodian)
    with pytest.raises(svc.RequestError, match="active members"):
        submit(make_user("student_member"))


def test_full_flow_triage_wait_reply_resolve_confirm(
    requester, custodian, submit, django_capture_on_commit_callbacks
):
    sr = submit(requester)
    with django_capture_on_commit_callbacks(execute=True):
        svc.triage(sr, actor=custodian, assignee=custodian, priority="high")
        svc.set_status(sr, actor=custodian, status="waiting", note="Which company is it for?")
        svc.add_comment(sr, actor=requester, body="Newmont Ghana")
    sr.refresh_from_db()
    assert sr.request_status == "in_progress"  # a reply moves it off "waiting"
    with django_capture_on_commit_callbacks(execute=True):
        svc.resolve(sr, actor=custodian, summary="Letter ready at the office.")
    sr.refresh_from_db()
    assert sr.request_status == "resolved" and sr.resolved_at
    svc.confirm_resolved(sr, actor=requester)
    sr.refresh_from_db()
    assert sr.request_status == "closed"
    kinds = set(
        Notification.objects.filter(user=requester, channel="in_app").values_list(
            "notification_type", flat=True
        )
    )
    assert {"service_request_waiting", "service_request_resolved"} <= kinds


def test_requester_can_reopen_a_resolution(requester, custodian, submit):
    sr = submit(requester)
    svc.resolve(sr, actor=custodian, summary="Done")
    with pytest.raises(svc.RequestError, match="still needs"):
        svc.reopen(sr, actor=requester, reason="")
    svc.reopen(sr, actor=requester, reason="The letter has the wrong date.")
    sr.refresh_from_db()
    assert sr.request_status == "in_progress" and sr.resolved_at is None


def test_withdraw(requester, custodian, submit):
    sr = submit(requester)
    with pytest.raises(svc.RequestError, match="Only the person"):
        svc.withdraw(sr, actor=custodian)
    svc.withdraw(sr, actor=requester)
    sr.refresh_from_db()
    assert sr.request_status == "closed"


def test_resolution_needs_an_explanation(requester, custodian, submit):
    with pytest.raises(svc.RequestError, match="Explain"):
        svc.resolve(submit(requester), actor=custodian, summary="  ")


def test_invalid_status_transition(requester, custodian, submit):
    sr = submit(requester)
    svc.resolve(sr, actor=custodian, summary="Done")
    with pytest.raises(svc.RequestError):
        svc.set_status(sr, actor=custodian, status="in_progress")


def test_audit_never_contains_request_text(requester, custodian, submit):
    sr = submit(requester, title="Grade missing", description="My SECRET-DETAIL here")
    svc.add_comment(sr, actor=custodian, body="COMMENT-TEXT", staff_only=True)
    svc.resolve(sr, actor=custodian, summary="RESOLUTION-TEXT")
    dumped = str(list(AuditEvent.objects.filter(entity_id=sr.pk).values("before_data", "after_data")))
    for secret in ("SECRET-DETAIL", "COMMENT-TEXT", "RESOLUTION-TEXT", "Grade missing"):
        assert secret not in dumped


# ----- staff-only comments ------------------------------------------------------------


def test_staff_only_comments_hidden_from_requester(requester, custodian, submit):
    sr = submit(requester)
    svc.add_comment(sr, actor=custodian, body="Visible reply")
    svc.add_comment(sr, actor=custodian, body="Job card 4411 raised", staff_only=True)
    assert [c.body for c in svc.visible_comments(requester, sr)] == ["Visible reply"]
    assert len(svc.visible_comments(custodian, sr)) == 2


def test_requester_cannot_post_staff_only(requester, submit):
    sr = submit(requester)
    with pytest.raises(svc.RequestError, match="staff-only"):
        svc.add_comment(sr, actor=requester, body="x", staff_only=True)


def test_officer_who_is_the_requester_does_not_see_staff_notes_on_own_request(president, custodian, submit):
    """The President holds requests.manage, but on their own request they're just the requester."""
    sr = submit(president)
    svc.add_comment(sr, actor=custodian, body="Internal note", staff_only=True)
    assert list(svc.visible_comments(president, sr)) == []
    assert not svc.can_handle(president, sr)
    with pytest.raises(svc.RequestError, match="your own request"):
        svc.triage(sr, actor=president, assignee=custodian)
    with pytest.raises(svc.RequestError, match="your own request"):
        svc.resolve(sr, actor=president, summary="Self-approved")


def test_staff_only_notes_do_not_notify_requester(
    requester, custodian, submit, django_capture_on_commit_callbacks
):
    sr = submit(requester)
    with django_capture_on_commit_callbacks(execute=True):
        svc.add_comment(sr, actor=custodian, body="internal", staff_only=True)
    assert not Notification.objects.filter(user=requester).exists()


def test_third_party_member_cannot_comment(requester, make_member, submit):
    sr = submit(requester)
    with pytest.raises(svc.RequestError, match="can't comment"):
        svc.add_comment(sr, actor=make_member(), body="hi")


# ----- restricted requests ------------------------------------------------------------


def test_restricted_request_goes_straight_to_handler(requester, hod, custodian, president, submit):
    sr = submit(requester, request_type="result_query", title="Grade missing")
    assert sr.visibility_level == "restricted"
    assert sr.assigned_to_user == hod
    assert sr in svc.visible_requests(hod, sr.department)
    assert sr in svc.visible_requests(requester, sr.department)
    # Officers with requests.manage but not the handling role never see it.
    for officer in (custodian, president):
        assert sr not in svc.visible_requests(officer, sr.department)
        assert not svc.can_handle(officer, sr)


def test_restricted_request_cannot_be_actioned_by_other_officers(requester, hod, custodian, submit):
    sr = submit(requester, request_type="result_query")
    with pytest.raises(PermissionDenied):
        svc.triage(sr, actor=custodian, priority="high")
    with pytest.raises(PermissionDenied):
        svc.resolve(sr, actor=custodian, summary="x")
    with pytest.raises(svc.RequestError, match="can't comment"):
        svc.add_comment(sr, actor=custodian, body="peek", staff_only=True)


def test_restricted_request_cannot_be_assigned_outside_handlers(requester, hod, custodian, submit):
    sr = submit(requester, request_type="result_query")
    with pytest.raises(svc.RequestError, match="designated handler"):
        svc.triage(sr, actor=hod, assignee=custodian)
    assert set(svc.assignable_users(sr)) == {hod}


def test_restricted_request_refused_when_nobody_can_handle_it(requester, submit):
    with pytest.raises(svc.RequestError, match="Nobody can take"):
        submit(requester, request_type="result_query")
    assert not ServiceRequest.objects.exists()


def test_restricted_notification_does_not_reveal_title(
    requester, hod, submit, django_capture_on_commit_callbacks
):
    with django_capture_on_commit_callbacks(execute=True):
        sr = submit(requester, request_type="result_query", title="Failed MATH 251")
        svc.resolve(sr, actor=hod, summary="Corrected")
    for n in Notification.objects.all():
        assert "MATH 251" not in n.title and "MATH 251" not in n.body


def test_second_hod_can_see_restricted_requests_of_their_type(requester, hod, make_user, submit):
    adviser = make_user("hod_faculty_adviser")
    sr = submit(requester, request_type="result_query")
    assert sr in svc.visible_requests(adviser, sr.department)


def test_ended_handler_loses_access(requester, hod, submit):
    sr = submit(requester, request_type="result_query")
    hod.role_assignments.update(assignment_status="ended", ends_at=timezone.now())
    from apps.access.services import clear_cache

    clear_cache(hod)
    assert sr not in svc.visible_requests(hod, sr.department)


def test_general_assignment_only_to_request_handlers(requester, custodian, make_member, submit):
    sr = submit(requester)
    with pytest.raises(svc.RequestError, match="officers who handle requests"):
        svc.triage(sr, actor=custodian, assignee=make_member("treasurer"))


# ----- summary ------------------------------------------------------------------------


def test_sla_summary(requester, custodian, submit):
    on_time = submit(requester)
    svc.resolve(on_time, actor=custodian, summary="ok")
    late = submit(requester, request_type="dues_query")
    ServiceRequest.objects.filter(pk=late.pk).update(
        submitted_at=timezone.now() - datetime.timedelta(days=10),
        due_at=timezone.now() - datetime.timedelta(days=7),
    )
    withdrawn = submit(requester)
    svc.withdraw(withdrawn, actor=requester)
    rows = {r["request_type"]: r for r in svc.sla_summary(ServiceRequest.objects.all())}
    assert rows["introductory_letter"]["resolved"] == 1  # withdrawn one excluded
    assert rows["introductory_letter"]["on_time_rate"] == 100
    assert rows["dues_query"]["open"] == 1 and rows["dues_query"]["overdue"] == 1
    assert ServiceRequestComment.objects.filter(service_request=on_time).count() == 1

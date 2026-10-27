"""Documents: upload, versions, review, visibility and authorised download.

There is no separate documents permission in the catalogue, so access maps onto existing codes:
- Stewards (committees.manage: secretary, president, executive officers) see every document in the
  department except other people's private ones, and review uploads.
- Uploaders are stewards, notice publishers, finance officers (for invoices and evidence) and
  committee chairs (for their own committee's documents).
- Members see approved documents shared with the department, and approved documents of
  committees they sit on.

Restricted documents stay private:
- "Only me" documents are visible to their uploader alone.
- Financial evidence (category "financial") is visible only to finance officers and the uploader,
  whatever visibility it was given.
- Committee documents are visible only to that committee's members and stewards.
- Drafts, pending and rejected uploads are visible only to the uploader and stewards.
Every download is authorised here and audited. Files live in private storage (see storage.py).
"""

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.access.services import has_any_perm, has_perm
from apps.audit.services import record
from apps.common.errors import BusinessError, require_perm
from apps.common.navigation import FINANCE_VIEWERS, SPENDING_VIEWERS
from apps.workspace.models import Committee
from apps.workspace.services import chaired_committee_ids, seated_committee_ids

from . import storage
from .models import Document

STEWARD_PERM = "committees.manage"
FINANCE_DOC_VIEWERS = tuple(sorted(set(FINANCE_VIEWERS) | set(SPENDING_VIEWERS)))
UPLOADER_PERMS = (STEWARD_PERM, "announcements.publish") + FINANCE_DOC_VIEWERS
FINANCIAL = "financial"
# Visibility levels offered in this phase. Collaboration and public sharing arrive with
# interdepartmental collaboration (Phase 4); until then they behave like "department".
OFFERED_VISIBILITY = (
    Document.Visibility.OWNER_ONLY,
    Document.Visibility.DEPARTMENT,
    Document.Visibility.COMMITTEE,
)
AUDIT_FIELDS = ("title", "category", "visibility", "review_status", "version_number", "committee_id")


class DocumentError(BusinessError):
    pass


def _audit(doc):
    values = {f: getattr(doc, f) for f in AUDIT_FIELDS}
    return {f: str(v) if v is not None else None for f, v in values.items()}


# ----- who can do what ----------------------------------------------------------------


def is_steward(user, department):
    return has_perm(user, STEWARD_PERM, department)


def is_finance_viewer(user, department):
    return has_any_perm(user, FINANCE_DOC_VIEWERS, department)


def uploadable_committees(user, department):
    qs = Committee.objects.filter(department=department, committee_status=Committee.Status.ACTIVE)
    if is_steward(user, department):
        return qs
    return qs.filter(pk__in=chaired_committee_ids(user, department))


def can_upload(user, department):
    return has_any_perm(user, UPLOADER_PERMS, department) or uploadable_committees(user, department).exists()


def visible_documents(user, department):
    """Documents in `department` the user may see. Everything else is a 404."""
    qs = Document.objects.filter(department=department)
    own = Q(uploaded_by=user)
    not_private = ~Q(visibility=Document.Visibility.OWNER_ONLY)
    if is_steward(user, department):
        # Every non-private document, whatever its review state or committee.
        general = not_private
    else:
        committees = seated_committee_ids(user, department) | chaired_committee_ids(user, department)
        shared = Q(
            visibility__in=[
                Document.Visibility.DEPARTMENT,
                Document.Visibility.COLLABORATION,
                Document.Visibility.PUBLIC,
            ]
        )
        committee_doc = Q(visibility=Document.Visibility.COMMITTEE, committee_id__in=committees)
        general = Q(review_status__in=[Document.Review.APPROVED, Document.Review.ARCHIVED]) & (
            shared | committee_doc
        )
    allowed = own | (general & ~Q(category=FINANCIAL))
    if is_finance_viewer(user, department):
        # Finance officers see every non-private piece of financial evidence, reviewed or not.
        allowed |= Q(category=FINANCIAL) & not_private
    return qs.filter(allowed)


def can_view(user, document):
    return visible_documents(user, document.department).filter(pk=document.pk).exists()


def can_review(user, document):
    return (
        is_steward(user, document.department)
        and document.uploaded_by_id != user.pk
        and document.review_status == Document.Review.PENDING_REVIEW
        and document.visibility != Document.Visibility.OWNER_ONLY
    )


def can_add_version(user, document):
    if document.review_status == Document.Review.ARCHIVED or not document.is_latest:
        return False
    if document.uploaded_by_id == user.pk:
        return True
    if document.visibility == Document.Visibility.OWNER_ONLY:
        return False
    if document.category == FINANCIAL:
        return is_finance_viewer(user, document.department)
    if is_steward(user, document.department):
        return True
    return document.committee_id is not None and document.committee_id in chaired_committee_ids(
        user, document.department
    )


# ----- upload -------------------------------------------------------------------------


def _check_placement(user, department, *, category, visibility, committee):
    if visibility not in OFFERED_VISIBILITY:
        raise DocumentError("Choose who can see the document.")
    if visibility == Document.Visibility.COMMITTEE:
        if committee is None:
            raise DocumentError("Choose the committee that can see it.")
        if not uploadable_committees(user, department).filter(pk=committee.pk).exists():
            raise DocumentError("You can only share documents with committees you manage.")
    elif committee is not None and committee.department_id != department.pk:
        raise DocumentError("That committee belongs to another department.")
    if category == FINANCIAL and not is_finance_viewer(user, department):
        raise DocumentError("Only finance officers can file financial evidence.")
    if not has_any_perm(user, UPLOADER_PERMS, department) and visibility != Document.Visibility.COMMITTEE:
        # Committee chairs without an officer role upload for their committee only.
        raise DocumentError("You can upload documents for the committees you chair.")


@transaction.atomic
def upload(department, *, actor, file, title, category, visibility, committee=None, submit_for_review=False):
    if not can_upload(actor, department):
        raise PermissionDenied("You can't upload documents in this department.")
    _check_placement(actor, department, category=category, visibility=visibility, committee=committee)
    try:
        stored = storage.save(file, department)
    except storage.UploadRejected as exc:
        raise DocumentError(str(exc)) from exc
    # If the database write fails, don't leave an orphaned file behind.
    try:
        doc = Document.objects.create(
            department=department,
            uploaded_by=actor,
            title=title.strip(),
            category=category,
            original_filename=_safe_name(file.name),
            storage_key=stored.storage_key,
            mime_type=stored.mime_type,
            file_size_bytes=stored.size,
            checksum_sha256=stored.checksum,
            visibility=visibility,
            committee=committee if visibility == Document.Visibility.COMMITTEE else None,
            review_status=_initial_status(visibility, submit_for_review),
            uploaded_at=timezone.now(),
        )
    except Exception:
        storage.delete(stored.storage_key)
        raise
    record("document.uploaded", doc, actor=actor, department=department, after=_audit(doc))
    return doc


def _initial_status(visibility, submit_for_review):
    if visibility == Document.Visibility.OWNER_ONLY:
        return Document.Review.APPROVED  # private: nobody else to review it for
    return Document.Review.PENDING_REVIEW if submit_for_review else Document.Review.DRAFT


def _safe_name(name):
    name = (name or "file").replace("\\", "/").rsplit("/", 1)[-1]
    return "".join(c for c in name if c.isprintable() and c not in '"<>|:*?')[:255] or "file"


@transaction.atomic
def add_version(previous, *, actor, file, submit_for_review=False):
    previous = Document.objects.select_for_update().get(pk=previous.pk)
    if not can_view(actor, previous) or not can_add_version(actor, previous):
        raise DocumentError("You can't add a version to this document.")
    try:
        stored = storage.save(file, previous.department)
    except storage.UploadRejected as exc:
        raise DocumentError(str(exc)) from exc
    try:
        doc = Document.objects.create(
            department=previous.department,
            uploaded_by=actor,
            title=previous.title,
            category=previous.category,
            original_filename=_safe_name(file.name),
            storage_key=stored.storage_key,
            mime_type=stored.mime_type,
            file_size_bytes=stored.size,
            checksum_sha256=stored.checksum,
            visibility=previous.visibility,
            committee=previous.committee,
            version_number=previous.version_number + 1,
            previous_version_document=previous,
            review_status=_initial_status(previous.visibility, submit_for_review),
            uploaded_at=timezone.now(),
        )
    except Exception:
        storage.delete(stored.storage_key)
        raise
    record(
        "document.version_added",
        doc,
        actor=actor,
        department=doc.department,
        after={**_audit(doc), "previous": str(previous.pk)},
    )
    return doc


# ----- review -------------------------------------------------------------------------


def _locked(document):
    return Document.objects.select_for_update().get(pk=document.pk)


@transaction.atomic
def submit_for_review(document, *, actor):
    document = _locked(document)
    if document.uploaded_by_id != actor.pk:
        raise DocumentError("Only the uploader can send a document for review.")
    if document.visibility == Document.Visibility.OWNER_ONLY:
        raise DocumentError("Private documents don't need review.")
    if document.review_status not in (Document.Review.DRAFT, Document.Review.REJECTED):
        raise DocumentError("Only drafts can be sent for review.")
    before = _audit(document)
    document.review_status = Document.Review.PENDING_REVIEW
    document.save(update_fields=["review_status"])
    record(
        "document.submitted",
        document,
        actor=actor,
        department=document.department,
        before=before,
        after=_audit(document),
    )
    return document


@transaction.atomic
def review(document, *, actor, approve, note=""):
    document = _locked(document)
    require_perm(actor, STEWARD_PERM, document.department)
    if document.uploaded_by_id == actor.pk:
        raise DocumentError("Someone other than the uploader must review a document.")
    if document.review_status != Document.Review.PENDING_REVIEW:
        raise DocumentError("This document isn't waiting for review.")
    before = _audit(document)
    document.review_status = Document.Review.APPROVED if approve else Document.Review.REJECTED
    document.save(update_fields=["review_status"])
    record(
        "document.approved" if approve else "document.rejected",
        document,
        actor=actor,
        department=document.department,
        before=before,
        after={**_audit(document), "note": note.strip()[:500]},
    )
    if approve and document.previous_version_document_id:
        # The approved version supersedes older approved ones.
        Document.objects.filter(
            pk=document.previous_version_document_id, review_status=Document.Review.APPROVED
        ).update(review_status=Document.Review.ARCHIVED, updated_at=timezone.now())
    return document


@transaction.atomic
def archive(document, *, actor):
    document = _locked(document)
    if document.uploaded_by_id != actor.pk and not is_steward(actor, document.department):
        raise DocumentError("Only the uploader or a document steward can archive this.")
    if document.review_status == Document.Review.ARCHIVED:
        raise DocumentError("Already archived.")
    before = _audit(document)
    document.review_status = Document.Review.ARCHIVED
    document.save(update_fields=["review_status"])
    record(
        "document.archived",
        document,
        actor=actor,
        department=document.department,
        before=before,
        after=_audit(document),
    )
    return document


# ----- download -----------------------------------------------------------------------


def open_for_download(document, *, actor):
    """Re-check access, audit the download and return an open file handle."""
    if not can_view(actor, document):
        raise PermissionDenied
    record(
        "document.downloaded",
        document,
        actor=actor,
        department=document.department,
        after={"version": document.version_number, "visibility": document.visibility},
    )
    return storage.open_file(document)


def versions(document, user):
    """The version chain the user may see, newest first, starting from any member of it."""
    root, seen = document, {document.pk}
    while root.previous_version_document_id and root.previous_version_document_id not in seen:
        root = root.previous_version_document
        seen.add(root.pk)
    chain, current = [root], root
    while (nxt := current.next_versions.order_by("version_number").first()) and nxt.pk not in seen:
        seen.add(nxt.pk)
        chain.append(nxt)
        current = nxt
    visible = set(
        visible_documents(user, document.department).filter(pk__in=seen).values_list("pk", flat=True)
    )
    return [d for d in reversed(chain) if d.pk in visible]

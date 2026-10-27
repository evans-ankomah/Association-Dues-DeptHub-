from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.finance.views.common import paginate

from . import services as svc
from .forms import UploadForm, VersionForm
from .models import CATEGORIES, Document


def _document(request, pk):
    if request.department is None:
        raise Http404
    return get_object_or_404(
        svc.visible_documents(request.user, request.department).select_related(
            "uploaded_by__profile", "committee", "previous_version_document"
        ),
        pk=pk,
    )


@login_required
def document_list(request):
    if request.department is None:
        raise PermissionDenied
    category = request.GET.get("category", "")
    status = request.GET.get("status", "")
    search = request.GET.get("q", "").strip()
    qs = (
        svc.visible_documents(request.user, request.department)
        .filter(next_versions__isnull=True)  # latest version of each document only
        .select_related("uploaded_by__profile", "committee")
    )
    if status:
        qs = qs.filter(review_status=status)
    else:
        qs = qs.exclude(review_status=Document.Review.ARCHIVED)
    if category:
        qs = qs.filter(category=category)
    if search:
        qs = qs.filter(Q(title__icontains=search) | Q(original_filename__icontains=search))
    page, query = paginate(request, qs.order_by("-uploaded_at"), per_page=30)
    steward = svc.is_steward(request.user, request.department)
    return render(
        request,
        "documents/document_list.html",
        {
            "page": page,
            "query": query,
            "filters": {"category": category, "status": status, "q": search},
            "categories": CATEGORIES,
            "statuses": Document.Review.choices,
            "can_upload": svc.can_upload(request.user, request.department),
            "awaiting_review": svc.visible_documents(request.user, request.department)
            .filter(review_status=Document.Review.PENDING_REVIEW)
            .exclude(uploaded_by=request.user)
            .count()
            if steward
            else 0,
        },
    )


@login_required
def upload(request):
    if request.department is None or not svc.can_upload(request.user, request.department):
        raise PermissionDenied
    committees = svc.uploadable_committees(request.user, request.department)
    form = UploadForm(request.POST or None, request.FILES or None, committees=committees)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        try:
            doc = svc.upload(
                request.department,
                actor=request.user,
                file=data["file"],
                title=data["title"],
                category=data["category"],
                visibility=data["visibility"],
                committee=data["committee"],
                submit_for_review="submit" in request.POST,
            )
        except svc.DocumentError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, "Document uploaded.")
            return redirect("documents:detail", pk=doc.pk)
    return render(request, "documents/upload.html", {"form": form})


@login_required
def document_detail(request, pk):
    doc = _document(request, pk)
    return render(
        request,
        "documents/document_detail.html",
        {
            "doc": doc,
            "versions": svc.versions(doc, request.user),
            "can_review": svc.can_review(request.user, doc),
            "can_add_version": svc.can_add_version(request.user, doc),
            "is_uploader": doc.uploaded_by_id == request.user.pk,
            "can_archive": doc.review_status != Document.Review.ARCHIVED
            and (doc.uploaded_by_id == request.user.pk or svc.is_steward(request.user, request.department)),
            "version_form": VersionForm(),
        },
    )


@login_required
def download(request, pk):
    doc = _document(request, pk)
    handle = svc.open_for_download(doc, actor=request.user)
    response = FileResponse(
        handle, as_attachment=True, filename=doc.original_filename, content_type=doc.mime_type
    )
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "private, no-store"
    return response


@login_required
@require_POST
def add_version(request, pk):
    doc = _document(request, pk)
    form = VersionForm(request.POST, request.FILES)
    if not form.is_valid():
        messages.error(request, "Choose a file.")
        return redirect("documents:detail", pk=pk)
    try:
        new = svc.add_version(
            doc,
            actor=request.user,
            file=form.cleaned_data["file"],
            submit_for_review="submit" in request.POST,
        )
    except svc.DocumentError as exc:
        messages.error(request, str(exc))
        return redirect("documents:detail", pk=pk)
    messages.success(request, f"Version {new.version_number} uploaded.")
    return redirect("documents:detail", pk=new.pk)


@login_required
@require_POST
def action(request, pk):
    doc = _document(request, pk)
    act = request.POST.get("action")
    try:
        if act == "submit":
            svc.submit_for_review(doc, actor=request.user)
            messages.success(request, "Sent for review.")
        elif act in ("approve", "reject"):
            svc.review(doc, actor=request.user, approve=act == "approve", note=request.POST.get("note", ""))
            messages.success(request, "Document approved." if act == "approve" else "Document rejected.")
        elif act == "archive":
            svc.archive(doc, actor=request.user)
            messages.success(request, "Document archived.")
        else:
            messages.error(request, "Unknown action.")
    except svc.DocumentError as exc:
        messages.error(request, str(exc))
    return redirect("documents:detail", pk=pk)
